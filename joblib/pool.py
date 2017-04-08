"""Custom implementation of multiprocessing.Pool with custom pickler.

This module provides efficient ways of working with data stored in
shared memory with numpy.memmap arrays without inducing any memory
copy between the parent and child processes.

This module should not be imported if multiprocessing is not
available as it implements subclasses of multiprocessing Pool
that uses a custom alternative to SimpleQueue.

"""
# Author: Olivier Grisel <olivier.grisel@ensta.org>
# Copyright: 2012, Olivier Grisel
# License: BSD 3 clause

from mmap import mmap
import errno
import os
import stat
import sys
import threading
import atexit
import tempfile
import shutil
import warnings
from time import sleep

try:
    WindowsError
except NameError:
    WindowsError = type(None)

from pickle import whichmodule
try:
    # Python 2 compat
    from cPickle import loads
    from cPickle import dumps
except ImportError:
    from pickle import loads
    from pickle import dumps
    import copyreg

# Customizable pure Python pickler in Python 2
# customizable C-optimized pickler under Python 3.3+
from pickle import Pickler

from pickle import HIGHEST_PROTOCOL
from io import BytesIO

from ._multiprocessing_helpers import mp, assert_spawning
# We need the class definition to derive from it not the multiprocessing.Pool
# factory function
from multiprocess.pool import Pool

try:
    import numpy as np
    from numpy.lib.stride_tricks import as_strided
except ImportError:
    np = None

from .numpy_pickle import load
from .numpy_pickle import dump
from .hashing import hash
from .backports import make_memmap
# Some system have a ramdisk mounted by default, we can use it instead of /tmp
# as the default folder to dump big arrays to share with subprocesses
SYSTEM_SHARED_MEM_FS = '/dev/shm'

# Folder and file permissions to chmod temporary files generated by the
# memmaping pool. Only the owner of the Python process can access the
# temporary files and folder.
FOLDER_PERMISSIONS = stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR
FILE_PERMISSIONS = stat.S_IRUSR | stat.S_IWUSR

###############################################################################
# Support for efficient transient pickling of numpy data structures


def _get_backing_memmap(a):
    """Recursively look up the original np.memmap instance base if any."""
    b = getattr(a, 'base', None)
    if b is None:
        # TODO: check scipy sparse datastructure if scipy is installed
        # a nor its descendants do not have a memmap base
        return None

    elif isinstance(b, mmap):
        # a is already a real memmap instance.
        return a

    else:
        # Recursive exploration of the base ancestry
        return _get_backing_memmap(b)


def _get_temp_dir(pool_folder_name, temp_folder=None):
    """Get the full path to a subfolder inside the temporary folder.

    Parameters
    ----------
    pool_folder_name : str
        Sub-folder name used for the serialization of a pool instance.

    temp_folder: str, optional
        Folder to be used by the pool for memmaping large arrays
        for sharing memory with worker processes. If None, this will try in
        order:

        - a folder pointed by the JOBLIB_TEMP_FOLDER environment
          variable,
        - /dev/shm if the folder exists and is writable: this is a
          RAMdisk filesystem available by default on modern Linux
          distributions,
        - the default system temporary folder that can be
          overridden with TMP, TMPDIR or TEMP environment
          variables, typically /tmp under Unix operating systems.

    Returns
    -------
    pool_folder : str
       full path to the temporary folder
    use_shared_mem : bool
       whether the temporary folder is written to tmpfs
    """
    use_shared_mem = False
    if temp_folder is None:
        temp_folder = os.environ.get('JOBLIB_TEMP_FOLDER', None)
    if temp_folder is None:
        if os.path.exists(SYSTEM_SHARED_MEM_FS):
            try:
                temp_folder = SYSTEM_SHARED_MEM_FS
                pool_folder = os.path.join(temp_folder, pool_folder_name)
                if not os.path.exists(pool_folder):
                    os.makedirs(pool_folder)
                use_shared_mem = True
            except IOError:
                # Missing rights in the the /dev/shm partition,
                # fallback to regular temp folder.
                temp_folder = None
    if temp_folder is None:
        # Fallback to the default tmp folder, typically /tmp
        temp_folder = tempfile.gettempdir()
    temp_folder = os.path.abspath(os.path.expanduser(temp_folder))
    pool_folder = os.path.join(temp_folder, pool_folder_name)
    return pool_folder, use_shared_mem


def has_shareable_memory(a):
    """Return True if a is backed by some mmap buffer directly or not."""
    return _get_backing_memmap(a) is not None


def _strided_from_memmap(filename, dtype, mode, offset, order, shape, strides,
                         total_buffer_len):
    """Reconstruct an array view on a memory mapped file."""
    if mode == 'w+':
        # Do not zero the original data when unpickling
        mode = 'r+'

    if strides is None:
        # Simple, contiguous memmap
        return make_memmap(filename, dtype=dtype, shape=shape, mode=mode,
                           offset=offset, order=order)
    else:
        # For non-contiguous data, memmap the total enclosing buffer and then
        # extract the non-contiguous view with the stride-tricks API
        base = make_memmap(filename, dtype=dtype, shape=total_buffer_len,
                           mode=mode, offset=offset, order=order)
        return as_strided(base, shape=shape, strides=strides)


def _reduce_memmap_backed(a, m):
    """Pickling reduction for memmap backed arrays.

    a is expected to be an instance of np.ndarray (or np.memmap)
    m is expected to be an instance of np.memmap on the top of the ``base``
    attribute ancestry of a. ``m.base`` should be the real python mmap object.
    """
    # offset that comes from the striding differences between a and m
    a_start, a_end = np.byte_bounds(a)
    m_start = np.byte_bounds(m)[0]
    offset = a_start - m_start

    # offset from the backing memmap
    offset += m.offset

    if m.flags['F_CONTIGUOUS']:
        order = 'F'
    else:
        # The backing memmap buffer is necessarily contiguous hence C if not
        # Fortran
        order = 'C'

    if a.flags['F_CONTIGUOUS'] or a.flags['C_CONTIGUOUS']:
        # If the array is a contiguous view, no need to pass the strides
        strides = None
        total_buffer_len = None
    else:
        # Compute the total number of items to map from which the strided
        # view will be extracted.
        strides = a.strides
        total_buffer_len = (a_end - a_start) // a.itemsize
    return (_strided_from_memmap,
            (m.filename, a.dtype, m.mode, offset, order, a.shape, strides,
             total_buffer_len))


def reduce_memmap(a):
    """Pickle the descriptors of a memmap instance to reopen on same file."""
    m = _get_backing_memmap(a)
    if m is not None:
        # m is a real mmap backed memmap instance, reduce a preserving striding
        # information
        return _reduce_memmap_backed(a, m)
    else:
        # This memmap instance is actually backed by a regular in-memory
        # buffer: this can happen when using binary operators on numpy.memmap
        # instances
        return (loads, (dumps(np.asarray(a), protocol=HIGHEST_PROTOCOL),))


class ArrayMemmapReducer(object):
    """Reducer callable to dump large arrays to memmap files.

    Parameters
    ----------
    max_nbytes: int
        Threshold to trigger memmaping of large arrays to files created
        a folder.
    temp_folder: str
        Path of a folder where files for backing memmaped arrays are created.
    mmap_mode: 'r', 'r+' or 'c'
        Mode for the created memmap datastructure. See the documentation of
        numpy.memmap for more details. Note: 'w+' is coerced to 'r+'
        automatically to avoid zeroing the data on unpickling.
    verbose: int, optional, 0 by default
        If verbose > 0, memmap creations are logged.
        If verbose > 1, both memmap creations, reuse and array pickling are
        logged.
    prewarm: bool, optional, False by default.
        Force a read on newly memmaped array to make sure that OS pre-cache it
        memory. This can be useful to avoid concurrent disk access when the
        same data array is passed to different worker processes.
    """

    def __init__(self, max_nbytes, temp_folder, mmap_mode, verbose=0,
                 context_id=None, prewarm=True):
        self._max_nbytes = max_nbytes
        self._temp_folder = temp_folder
        self._mmap_mode = mmap_mode
        self.verbose = int(verbose)
        self._prewarm = prewarm
        if context_id is not None:
            warnings.warn('context_id is deprecated and ignored in joblib'
                          ' 0.9.4 and will be removed in 0.11',
                          DeprecationWarning)

    def __call__(self, a):
        m = _get_backing_memmap(a)
        if m is not None:
            # a is already backed by a memmap file, let's reuse it directly
            return _reduce_memmap_backed(a, m)

        if (not a.dtype.hasobject
                and self._max_nbytes is not None
                and a.nbytes > self._max_nbytes):
            # check that the folder exists (lazily create the pool temp folder
            # if required)
            try:
                os.makedirs(self._temp_folder)
                os.chmod(self._temp_folder, FOLDER_PERMISSIONS)
            except OSError as e:
                if e.errno != errno.EEXIST:
                    raise e

            # Find a unique, concurrent safe filename for writing the
            # content of this array only once.
            basename = "%d-%d-%s.pkl" % (
                os.getpid(), id(threading.current_thread()), hash(a))
            filename = os.path.join(self._temp_folder, basename)

            # In case the same array with the same content is passed several
            # times to the pool subprocess children, serialize it only once

            # XXX: implement an explicit reference counting scheme to make it
            # possible to delete temporary files as soon as the workers are
            # done processing this data.
            if not os.path.exists(filename):
                if self.verbose > 0:
                    print("Memmaping (shape=%r, dtype=%s) to new file %s" % (
                        a.shape, a.dtype, filename))
                for dumped_filename in dump(a, filename):
                    os.chmod(dumped_filename, FILE_PERMISSIONS)

                if self._prewarm:
                    # Warm up the data to avoid concurrent disk access in
                    # multiple children processes
                    load(filename, mmap_mode=self._mmap_mode).max()
            elif self.verbose > 1:
                print("Memmaping (shape=%s, dtype=%s) to old file %s" % (
                    a.shape, a.dtype, filename))

            # The worker process will use joblib.load to memmap the data
            return (load, (filename, self._mmap_mode))
        else:
            # do not convert a into memmap, let pickler do its usual copy with
            # the default system pickler
            if self.verbose > 1:
                print("Pickling array (shape=%r, dtype=%s)." % (
                    a.shape, a.dtype))
            return (loads, (dumps(a, protocol=HIGHEST_PROTOCOL),))


###############################################################################
# Enable custom pickling in Pool queues

class CustomizablePickler(Pickler):
    """Pickler that accepts custom reducers.

    HIGHEST_PROTOCOL is selected by default as this pickler is used
    to pickle ephemeral datastructures for interprocess communication
    hence no backward compatibility is required.

    `reducers` is expected to be a dictionary with key/values
    being `(type, callable)` pairs where `callable` is a function that
    give an instance of `type` will return a tuple `(constructor,
    tuple_of_objects)` to rebuild an instance out of the pickled
    `tuple_of_objects` as would return a `__reduce__` method. See the
    standard library documentation on pickling for more details.

    """

    # We override the pure Python pickler as its the only way to be able to
    # customize the dispatch table without side effects in Python 2.7
    # to 3.2. For Python 3.3+ leverage the new dispatch_table
    # feature from http://bugs.python.org/issue14166 that makes it possible
    # to use the C implementation of the Pickler which is faster.

    def __init__(self, writer, reducers=None, protocol=HIGHEST_PROTOCOL):
        Pickler.__init__(self, writer, protocol=protocol)
        if reducers is None:
            reducers = {}
        if hasattr(Pickler, 'dispatch'):
            # Make the dispatch registry an instance level attribute instead of
            # a reference to the class dictionary under Python 2
            self.dispatch = Pickler.dispatch.copy()
        else:
            # Under Python 3 initialize the dispatch table with a copy of the
            # default registry
            self.dispatch_table = copyreg.dispatch_table.copy()
        for type, reduce_func in reducers.items():
            self.register(type, reduce_func)

    def register(self, type, reduce_func):
        """Attach a reducer function to a given type in the dispatch table."""
        if hasattr(Pickler, 'dispatch'):
            # Python 2 pickler dispatching is not explicitly customizable.
            # Let us use a closure to workaround this limitation.
            def dispatcher(self, obj):
                reduced = reduce_func(obj)
                self.save_reduce(obj=obj, *reduced)
            self.dispatch[type] = dispatcher
        else:
            self.dispatch_table[type] = reduce_func


class CustomizablePicklingQueue(object):
    """Locked Pipe implementation that uses a customizable pickler.

    This class is an alternative to the multiprocessing implementation
    of SimpleQueue in order to make it possible to pass custom
    pickling reducers, for instance to avoid memory copy when passing
    memory mapped datastructures.

    `reducers` is expected to be a dict with key / values being
    `(type, callable)` pairs where `callable` is a function that, given an
    instance of `type`, will return a tuple `(constructor, tuple_of_objects)`
    to rebuild an instance out of the pickled `tuple_of_objects` as would
    return a `__reduce__` method.

    See the standard library documentation on pickling for more details.
    """

    def __init__(self, context, reducers=None):
        self._reducers = reducers
        self._reader, self._writer = context.Pipe(duplex=False)
        self._rlock = context.Lock()
        if sys.platform == 'win32':
            self._wlock = None
        else:
            self._wlock = context.Lock()
        self._make_methods()

    def __getstate__(self):
        assert_spawning(self)
        return (self._reader, self._writer, self._rlock, self._wlock,
                self._reducers)

    def __setstate__(self, state):
        (self._reader, self._writer, self._rlock, self._wlock,
         self._reducers) = state
        self._make_methods()

    def empty(self):
        return not self._reader.poll()

    def _make_methods(self):
        self._recv = recv = self._reader.recv
        racquire, rrelease = self._rlock.acquire, self._rlock.release

        def get():
            racquire()
            try:
                return recv()
            finally:
                rrelease()

        self.get = get

        if self._reducers:
            def send(obj):
                buffer = BytesIO()
                CustomizablePickler(buffer, self._reducers).dump(obj)
                self._writer.send_bytes(buffer.getvalue())
            self._send = send
        else:
            self._send = send = self._writer.send
        if self._wlock is None:
            # writes to a message oriented win32 pipe are atomic
            self.put = send
        else:
            wlock_acquire, wlock_release = (
                self._wlock.acquire, self._wlock.release)

            def put(obj):
                wlock_acquire()
                try:
                    return send(obj)
                finally:
                    wlock_release()

            self.put = put


class PicklingPool(Pool):
    """Pool implementation with customizable pickling reducers.

    This is useful to control how data is shipped between processes
    and makes it possible to use shared memory without useless
    copies induces by the default pickling methods of the original
    objects passed as arguments to dispatch.

    `forward_reducers` and `backward_reducers` are expected to be
    dictionaries with key/values being `(type, callable)` pairs where
    `callable` is a function that, given an instance of `type`, will return a
    tuple `(constructor, tuple_of_objects)` to rebuild an instance out of the
    pickled `tuple_of_objects` as would return a `__reduce__` method.
    See the standard library documentation about pickling for more details.

    """

    def __init__(self, processes=None, forward_reducers=None,
                 backward_reducers=None, **kwargs):
        if forward_reducers is None:
            forward_reducers = dict()
        if backward_reducers is None:
            backward_reducers = dict()
        self._forward_reducers = forward_reducers
        self._backward_reducers = backward_reducers
        poolargs = dict(processes=processes)
        poolargs.update(kwargs)
        super(PicklingPool, self).__init__(**poolargs)

    def _setup_queues(self):
        context = getattr(self, '_ctx', mp)
        self._inqueue = CustomizablePicklingQueue(context,
                                                  self._forward_reducers)
        self._outqueue = CustomizablePicklingQueue(context,
                                                   self._backward_reducers)
        self._quick_put = self._inqueue._send
        self._quick_get = self._outqueue._recv


def delete_folder(folder_path):
    """Utility function to cleanup a temporary folder if still existing."""
    try:
        if os.path.exists(folder_path):
            shutil.rmtree(folder_path)
    except WindowsError:
        warnings.warn("Failed to clean temporary folder: %s" % folder_path)


class MemmapingPool(PicklingPool):
    """Process pool that shares large arrays to avoid memory copy.

    This drop-in replacement for `multiprocessing.pool.Pool` makes
    it possible to work efficiently with shared memory in a numpy
    context.

    Existing instances of numpy.memmap are preserved: the child
    suprocesses will have access to the same shared memory in the
    original mode except for the 'w+' mode that is automatically
    transformed as 'r+' to avoid zeroing the original data upon
    instantiation.

    Furthermore large arrays from the parent process are automatically
    dumped to a temporary folder on the filesystem such as child
    processes to access their content via memmaping (file system
    backed shared memory).

    Note: it is important to call the terminate method to collect
    the temporary folder used by the pool.

    Parameters
    ----------
    processes: int, optional
        Number of worker processes running concurrently in the pool.
    initializer: callable, optional
        Callable executed on worker process creation.
    initargs: tuple, optional
        Arguments passed to the initializer callable.
    temp_folder: str, optional
        Folder to be used by the pool for memmaping large arrays
        for sharing memory with worker processes. If None, this will try in
        order:
        - a folder pointed by the JOBLIB_TEMP_FOLDER environment variable,
        - /dev/shm if the folder exists and is writable: this is a RAMdisk
          filesystem available by default on modern Linux distributions,
        - the default system temporary folder that can be overridden
          with TMP, TMPDIR or TEMP environment variables, typically /tmp
          under Unix operating systems.
    max_nbytes int or None, optional, 1e6 by default
        Threshold on the size of arrays passed to the workers that
        triggers automated memory mapping in temp_folder.
        Use None to disable memmaping of large arrays.
    mmap_mode: {'r+', 'r', 'w+', 'c'}
        Memmapping mode for numpy arrays passed to workers.
        See 'max_nbytes' parameter documentation for more details.
    forward_reducers: dictionary, optional
        Reducers used to pickle objects passed from master to worker
        processes: see below.
    backward_reducers: dictionary, optional
        Reducers used to pickle return values from workers back to the
        master process.
    verbose: int, optional
        Make it possible to monitor how the communication of numpy arrays
        with the subprocess is handled (pickling or memmaping)
    prewarm: bool or str, optional, "auto" by default.
        If True, force a read on newly memmaped array to make sure that OS pre-
        cache it in memory. This can be useful to avoid concurrent disk access
        when the same data array is passed to different worker processes.
        If "auto" (by default), prewarm is set to True, unless the Linux shared
        memory partition /dev/shm is available and used as temp_folder.

    `forward_reducers` and `backward_reducers` are expected to be
    dictionaries with key/values being `(type, callable)` pairs where
    `callable` is a function that give an instance of `type` will return
    a tuple `(constructor, tuple_of_objects)` to rebuild an instance out
    of the pickled `tuple_of_objects` as would return a `__reduce__`
    method. See the standard library documentation on pickling for more
    details.

    """

    def __init__(self, processes=None, temp_folder=None, max_nbytes=1e6,
                 mmap_mode='r', forward_reducers=None, backward_reducers=None,
                 verbose=0, context_id=None, prewarm=False, **kwargs):
        if forward_reducers is None:
            forward_reducers = dict()
        if backward_reducers is None:
            backward_reducers = dict()
        if context_id is not None:
            warnings.warn('context_id is deprecated and ignored in joblib'
                          ' 0.9.4 and will be removed in 0.11',
                          DeprecationWarning)

        # Prepare a sub-folder name for the serialization of this particular
        # pool instance (do not create in advance to spare FS write access if
        # no array is to be dumped):
        pool_folder_name = "joblib_memmaping_pool_%d_%d" % (
            os.getpid(), id(self))
        pool_folder, use_shared_mem = _get_temp_dir(pool_folder_name,
                                                    temp_folder)
        self._temp_folder = pool_folder

        # Register the garbage collector at program exit in case caller forgets
        # to call terminate explicitly: note we do not pass any reference to
        # self to ensure that this callback won't prevent garbage collection of
        # the pool instance and related file handler resources such as POSIX
        # semaphores and pipes
        pool_module_name = whichmodule(delete_folder, 'delete_folder')

        def _cleanup():
            # In some cases the Python runtime seems to set delete_folder to
            # None just before exiting when accessing the delete_folder
            # function from the closure namespace. So instead we reimport
            # the delete_folder function explicitly.
            # https://github.com/joblib/joblib/issues/328
            # We cannot just use from 'joblib.pool import delete_folder'
            # because joblib should only use relative imports to allow
            # easy vendoring.
            delete_folder = __import__(
                pool_module_name, fromlist=['delete_folder']).delete_folder
            delete_folder(pool_folder)

        atexit.register(_cleanup)

        if np is not None:
            # Register smart numpy.ndarray reducers that detects memmap backed
            # arrays and that is alse able to dump to memmap large in-memory
            # arrays over the max_nbytes threshold
            if prewarm == "auto":
                prewarm = not use_shared_mem
            forward_reduce_ndarray = ArrayMemmapReducer(
                max_nbytes, pool_folder, mmap_mode, verbose,
                prewarm=prewarm)
            forward_reducers[np.ndarray] = forward_reduce_ndarray
            forward_reducers[np.memmap] = reduce_memmap

            # Communication from child process to the parent process always
            # pickles in-memory numpy.ndarray without dumping them as memmap
            # to avoid confusing the caller and make it tricky to collect the
            # temporary folder
            backward_reduce_ndarray = ArrayMemmapReducer(
                None, pool_folder, mmap_mode, verbose)
            backward_reducers[np.ndarray] = backward_reduce_ndarray
            backward_reducers[np.memmap] = reduce_memmap

        poolargs = dict(
            processes=processes,
            forward_reducers=forward_reducers,
            backward_reducers=backward_reducers)
        poolargs.update(kwargs)
        super(MemmapingPool, self).__init__(**poolargs)

    def terminate(self):
        n_retries = 10
        for i in range(n_retries):
            try:
                super(MemmapingPool, self).terminate()
                break
            except OSError as e:
                if isinstance(e, WindowsError):
                    # Workaround  occasional "[Error 5] Access is denied" issue
                    # when trying to terminate a process under windows.
                    sleep(0.1)
                    if i + 1 == n_retries:
                        warnings.warn("Failed to terminate worker processes in"
                                      " multiprocessing pool: %r" % e)
        delete_folder(self._temp_folder)
