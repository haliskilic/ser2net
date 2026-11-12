"""Where log records go besides all.log: the console, and other libraries' records.

The console copy of the ser2net log (stdout) is written by a thread of its own
(ConsoleQueueHandler): a console that doesn't take output (a click into a Windows console
window with QuickEdit on pauses it until Esc or Enter; a paused terminal; a pipe nobody
reads) must not hold up the event loop, and with it every bridge. A line is still on the
console when log() returns, as before, as long as the console takes it within
CONSOLE_WAIT_S; once it doesn't, nobody waits for it until it has caught up: its lines wait
in a bounded queue, past that they are dropped and counted, and the next line the console
takes says how many (all.log, written as before, has them all).

The formatter that makes a console line is the one the caller passes, the same object as
all.log's: whatever format all.log uses, the console uses too.
"""
from __future__ import annotations

import copy
import logging
import queue
import threading
from logging.handlers import QueueHandler
from typing import Optional

# console lines waiting while the console doesn't take them (then dropped and counted)
CONSOLE_QUEUE_MAX = 5000
# how long log() waits for the console to take a line (then not again until it caught up)
CONSOLE_WAIT_S = 0.05
# how long close() waits for the console to take what is still queued (at exit)
CONSOLE_STOP_WAIT_S = 2.0

# turns an exception into its traceback text (the record keeps it as exc_text)
_TRACEBACK = logging.Formatter()


class ConsoleQueueHandler(QueueHandler):
    """Hands each record to a bounded queue that one daemon thread (ConsoleListener)
    writes to ``target``, and waits up to ``wait`` seconds for it to be written; when it
    isn't, the console is ``behind`` and nobody waits until the queue is empty again.
    Never raises: a record the queue has no room for (or that can't be prepared) is
    counted, not reported to stderr (handleError would write there, which may be the
    blocked console itself)."""

    def __init__(self, target: logging.Handler, maxsize: int = CONSOLE_QUEUE_MAX,
                 wait: float = CONSOLE_WAIT_S) -> None:
        self.items: queue.Queue = queue.Queue(maxsize)
        super().__init__(self.items)
        self.target = target
        self.wait = wait
        self.behind = False
        self.dropped = 0
        self._count = threading.Lock()
        self.console = ConsoleListener(self)
        self.console.start()

    def handle(self, record: logging.LogRecord):
        """emit() without the handler's lock (the queue has its own): a thread waiting for
        a slow console doesn't make another one wait for the lock as well."""
        rv = self.filter(record)
        if isinstance(rv, logging.LogRecord):  # (a filter may return a changed record)
            record = rv
        if rv:
            self.emit(record)
        return rv

    def prepare(self, record: logging.LogRecord) -> logging.LogRecord:
        """A copy of the record with its message as text and a traceback as exc_text (the
        target's formatter writes it after the line, as for all.log; the stdlib's
        prepare() would make it part of the message)."""
        record = copy.copy(record)
        record.msg = record.getMessage()
        record.args = None
        if record.exc_info:
            record.exc_text = record.exc_text or _TRACEBACK.formatException(record.exc_info)
            record.exc_info = None
        return record

    def enqueue(self, record: logging.LogRecord) -> None:
        done = None if self.behind else threading.Event()
        try:
            self.items.put_nowait((record, done))
        except queue.Full:
            self._drop()
            return
        if done is not None and not done.wait(self.wait):
            self.behind = True

    def handleError(self, record: logging.LogRecord) -> None:  # noqa: N802 (logging API)
        self._drop()

    def _drop(self) -> None:
        with self._count:
            self.dropped += 1

    def take_dropped(self) -> int:
        """The records dropped since the last call."""
        with self._count:
            n, self.dropped = self.dropped, 0
        return n

    def close(self) -> None:
        """Let the console take what is queued (at most CONSOLE_STOP_WAIT_S), then stop
        its thread. Called at exit (logging.shutdown) and when the log is set up again."""
        self.console.stop()
        super().close()


class ConsoleListener:
    """The thread that writes the queued records to the console. Before a record, it
    writes how many were dropped since the last one, if any."""

    def __init__(self, source: ConsoleQueueHandler) -> None:
        self.source = source
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="console-log", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        items = self.source.items
        while (item := items.get()) is not None:
            self.handle(*item)

    def handle(self, record: logging.LogRecord, done: Optional[threading.Event]) -> None:
        target = self.source.target
        dropped = self.source.take_dropped()
        if dropped:
            note = copy.copy(record)
            note.msg = (f"... {dropped} console line(s) dropped: the console did not take "
                        "them (all.log has them)")
            note.args, note.exc_text, note.stack_info = None, None, None
            target.handle(note)
        target.handle(record)
        if done is not None:
            done.set()
        if self.source.items.empty():
            self.source.behind = False  # caught up: wait for it again

    def stop(self, timeout: float = CONSOLE_STOP_WAIT_S) -> None:
        thread = self._thread
        if thread is None:
            return
        self._thread = None
        try:
            self.source.items.put(None, timeout=timeout)
        except queue.Full:  # the console still doesn't take anything: leave it
            return
        thread.join(timeout)


def console_handler(stream, formatter: logging.Formatter) -> ConsoleQueueHandler:
    """The console copy of a log: ``stream`` (stdout) written with ``formatter`` by a
    thread of its own (ConsoleQueueHandler)."""
    target = logging.StreamHandler(stream)
    target.setFormatter(formatter)
    return ConsoleQueueHandler(target)


def close_console(logger: logging.Logger) -> None:
    """Stop the console threads of ``logger``'s handlers (before they are replaced)."""
    for handler in list(logger.handlers):
        if isinstance(handler, ConsoleQueueHandler):
            handler.close()
