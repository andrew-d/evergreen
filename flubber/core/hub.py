# -*- coding: utf-8 -
#
# This file is part of flubber. See the NOTICE for more information.

import pyuv
import traceback

from collections import deque
from functools import partial

from flubber import patcher
from flubber.core._greenlet import greenlet, get_current, GreenletExit
from flubber.threadpool import ThreadPool


__all__ = ["get_hub"]


threading = patcher.original('threading')
_tls = threading.local()


def get_hub():
    """Get the current event hub singleton object.
    """
    try:
        return _tls.hub
    except AttributeError:
        raise RuntimeError('there is no hub created in the current thread')


class Waker(object):

    def __init__(self, hub):
        self._async = pyuv.Async(hub.loop, lambda x: None)
        self._async.unref()

    def wake(self):
        self._async.send()


class Hub(object):
    SYSTEM_ERROR = (KeyboardInterrupt, SystemExit, SystemError)
    NOT_ERROR = (GreenletExit, SystemExit)

    def __init__(self):
        global _tls
        if getattr(_tls, 'hub', None) is not None:
            raise RuntimeError('cannot instantiate more than one Hub per thread')
        _tls.hub = self
        self.greenlet = greenlet(self._run_loop)
        self.loop = pyuv.Loop()
        self.loop.excepthook = self._handle_error
        self.threadpool = ThreadPool(self)

        self._timers = set()
        self._waker = Waker(self)
        self._signal_checker = pyuv.SignalChecker(self.loop)
        self._tick_prepare = pyuv.Prepare(self.loop)
        self._tick_idle = pyuv.Idle(self.loop)
        self._tick_callbacks = deque()

    def switch(self):
        current = get_current()
        switch_out = getattr(current, 'switch_out', None)
        if switch_out is not None:
            switch_out()
        try:
            if self.greenlet.parent is not current:
                current.parent = self.greenlet
        except ValueError:
            pass  # gets raised if there is a greenlet parent cycle
        return self.greenlet.switch()

    def switch_out(self):
        raise RuntimeError('Cannot switch to MAINLOOP from MAINLOOP')

    def next_tick(self, func, *args, **kw):
        self._tick_callbacks.append(partial(func, *args, **kw))
        if not self._tick_prepare.active:
            self._tick_prepare.start(self._tick_cb)
            self._tick_idle.start(lambda handle: handle.stop())

    def join(self):
        current = get_current()
        if current is not self.greenlet.parent:
            raise RuntimeError('run() can only be called from MAIN greenlet')
        if self.greenlet.dead:
            raise RuntimeError('hub has already ended')
        self.greenlet.switch()

    def destroy(self):
        global _tls
        try:
            hub = _tls.hub
        except AttributeError:
            raise RuntimeError('hub is already destroyed')
        else:
            if hub is not self:
                raise RuntimeError('destroy() can only be called from the same thread were the hub was created')
            del _tls.hub, hub

        self._cleanup_loop()
        self.loop.excepthook = None
        self.loop = None
        self.threadpool = None

        self._timers = None
        self._waker = None
        self._signal_checker = None
        self._tick_prepare = None
        self._tick_idle = None
        self._tick_callbacks = None

    def call_later(self, seconds, cb, *args, **kw):
        """Schedule a callable to be called after 'seconds' seconds have
        elapsed.
            seconds: The number of seconds to wait.
            cb: The callable to call after the given time.
            *args: Arguments to pass to the callable when called.
            **kw: Keyword arguments to pass to the callable when called.
        """
        return _Timer(self, seconds, cb, *args, **kw)

    def call_from_thread(self, func, *args, **kw):
        """Run the given callable in the hub thread. This is the only thread-safe
        function and the one that must be used to call any cooperative function from
        a thread other than the one running the hub.
        """
        async = None
        def _cb(handle):
            try:
                func(*args, **kw)
            finally:
                async.close()
        async = pyuv.Async(self.loop, _cb)
        async.send()

    # internal

    def _handle_error(self, typ, value, tb):
        if not issubclass(typ, self.NOT_ERROR):
            traceback.print_exception(typ, value, tb)
        if issubclass(typ, self.SYSTEM_ERROR):
            current = get_current()
            if current is self.greenlet:
                self.greenlet.parent.throw(typ, value)
            else:
                self.next_tick(self.parent.throw, typ, value)
        del tb

    def _run_loop(self):
        self._signal_checker.start()
        try:
            self.loop.run()
        finally:
            self._cleanup_loop()

    def _cleanup_loop(self):
        def cb(handle):
            if not handle.closed:
                handle.close()
        self.loop.walk(cb)
        # All handles are now closed, run will not block
        self.loop.run()

    def _tick_cb(self, handle):
        self._tick_prepare.stop()
        self._tick_idle.stop()
        queue, self._tick_callbacks = self._tick_callbacks, deque()
        for f in queue:
            f()


class _Timer(object):

    def __init__(self, hub, seconds, cb, *args, **kw):
        self.called = False
        self.cb = partial(cb, *args, **kw)
        hub._timers.add(self)
        self._timer = pyuv.Timer(hub.loop)
        self._timer.start(self._timer_cb, seconds, 0.0)

    def _timer_cb(self, timer):
        if not self.called:
            self.called = True
            try:
                self.cb()
            finally:
                self.cb = None
                self._timer.close()
                self._timer = None
                hub = get_hub()
                hub._timers.remove(self)

    @property
    def pending(self):
        return not self.called

    def cancel(self):
        if not self.called:
            self.called = True
            self._timer.close()
            self._timer = None
            hub = get_hub()
            hub._timers.remove(self)
