"""A stand-in for pyserial's serial.win32, to drive serial_compat.OverlappedWrites on any OS.

WriteFile starts a write that stays pending (ERROR_IO_PENDING) until the test lets the
"device" take it (complete), the port is purged (purge: ERROR_OPERATION_ABORTED, as
PurgeComm does) or the write fails (fail_next). GetOverlappedResult, CancelIoEx,
WaitForSingleObject, CreateEvent and CloseHandle answer as Windows does for such writes;
every write keeps the very data object WriteFile was given, so a test can check that the
caller kept it alive.
"""
import ctypes


class FakeWin32:
    ERROR_SUCCESS = 0
    ERROR_NOT_ENOUGH_MEMORY = 8
    ERROR_GEN_FAILURE = 31
    ERROR_OPERATION_ABORTED = 995
    ERROR_IO_INCOMPLETE = 996
    ERROR_IO_PENDING = 997
    ERROR_INVALID_USER_BUFFER = 1784
    DWORD = ctypes.c_ulong

    class OVERLAPPED(ctypes.Structure):
        _fields_ = [("Internal", ctypes.c_void_p), ("InternalHigh", ctypes.c_void_p),
                    ("Offset", ctypes.c_ulong), ("OffsetHigh", ctypes.c_ulong),
                    ("hEvent", ctypes.c_void_p)]

    def __init__(self):
        self.writes = []         # [OVERLAPPED, data, status]: None = pending, 0 = done, code
        self.signaled = {}       # event handle -> set
        self.closed = set()      # event handles closed
        self.refuse = None       # WriteFile fails at once with this error code
        self.ignore_cancel = False
        self.taken = bytearray()  # what the device took, in order
        self._last_error = 0
        self._next = 100
        self._credit = 0

    # ---- the API serial_compat uses ----
    def GetLastError(self):
        return self._last_error

    def CreateEvent(self, _attrs, _manual, _initial, _name):
        self._next += 1
        self.signaled[self._next] = False
        return self._next

    def CloseHandle(self, handle):
        self.closed.add(handle)
        return True

    def WriteFile(self, _handle, data, size, _done, overlapped):
        assert size == len(data)
        if self.refuse is not None:
            self._last_error = self.refuse
            return False
        self.writes.append([overlapped._obj, data, None])
        self._last_error = self.ERROR_IO_PENDING
        return False

    def GetOverlappedResult(self, _handle, overlapped, done, _wait):
        w = self._find(overlapped._obj)
        if w[2] is None:
            self._last_error = self.ERROR_IO_INCOMPLETE
            return False
        if w[2] == 0:
            done._obj.value = len(w[1])
            return True
        self._last_error = w[2]
        return False

    def CancelIoEx(self, _handle, overlapped):
        w = self._find(overlapped._obj)
        if w[2] is None and not self.ignore_cancel:
            self._end(w, self.ERROR_OPERATION_ABORTED)
        return True

    def WaitForSingleObject(self, event, _ms):
        return 0 if self.signaled.get(event) else 0x102  # WAIT_OBJECT_0 / WAIT_TIMEOUT

    # ---- the test's side ----
    def pending(self):
        return [w for w in self.writes if w[2] is None]

    def complete(self, nbytes):
        """The device takes ``nbytes`` more: pending writes complete, oldest first, once all
        of a write has been taken (what is left of ``nbytes`` counts towards the next one).
        How many bytes of writes completed."""
        self._credit += nbytes
        took = 0
        for w in self.pending():
            if len(w[1]) > self._credit:
                break
            self._credit -= len(w[1])
            took += len(w[1])
            self.taken += w[1]
            self._end(w, 0)
        if not self.pending():
            self._credit = 0  # an idle device does not save up
        return took

    def purge(self):
        for w in self.pending():
            self._end(w, self.ERROR_OPERATION_ABORTED)

    def fail_next(self, code=ERROR_GEN_FAILURE):
        self._end(self.pending()[0], code)

    def _end(self, w, status):
        w[2] = status
        self.signaled[w[0].hEvent] = True

    def _find(self, overlapped):
        for w in self.writes:
            if w[0] is overlapped:
                return w
        raise AssertionError("an OVERLAPPED that WriteFile never got")
