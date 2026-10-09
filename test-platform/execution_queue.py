"""Process-local FIFO queue and shared admission limit for catalog execution."""
import collections,threading
from contextlib import contextmanager
class QueueFull(RuntimeError):pass
class BoundedExecutor:
    def __init__(self,workers=2,capacity=32):
        if type(workers) is not int or not 1<=workers<=16 or type(capacity) is not int or not 1<=capacity<=1000:raise ValueError('invalid worker/queue limits')
        self.workers=workers;self.capacity=capacity;self._condition=threading.Condition();self._queue=collections.deque();self._running=0;self._closed=False;self._local=threading.local()
        self._threads=[threading.Thread(target=self._worker,daemon=True,name='platform-executor-'+str(i)) for i in range(workers)]
        for thread in self._threads:thread.start()
    def submit(self,key,operation):
        with self._condition:
            if self._closed or len(self._queue)>=self.capacity:raise QueueFull('execution queue full or closed; operation was not started')
            self._queue.append((key,operation));self._condition.notify_all()
    def cancel_queued(self,key):
        with self._condition:
            before=len(self._queue);self._queue=collections.deque((k,f) for k,f in self._queue if k!=key)
            self._condition.notify_all();return before-len(self._queue)
    def snapshot(self):
        with self._condition:return {'scope':'process','workers':self.workers,'queueCapacity':self.capacity,'queued':len(self._queue),'running':self._running,'closed':self._closed}
    @contextmanager
    def synchronous_slot(self):
        if getattr(self._local,'active',False):yield;return
        with self._condition:
            if self._closed or self._running>=self.workers or self._queue:raise QueueFull('execution capacity busy; operation was not started')
            self._running+=1
        self._local.active=True
        try:yield
        finally:
            self._local.active=False
            with self._condition:self._running-=1;self._condition.notify_all()
    def _worker(self):
        while True:
            with self._condition:
                while not (self._queue and self._running<self.workers):
                    if self._closed and not self._queue:return
                    self._condition.wait()
                key,operation=self._queue.popleft();self._running+=1
            self._local.active=True
            try:operation()
            except BaseException:pass  # Submitted job wrapper owns persistence/error reporting.
            finally:
                self._local.active=False
                with self._condition:self._running-=1;self._condition.notify_all()
    def shutdown(self,wait=True):
        with self._condition:self._closed=True;self._condition.notify_all()
        if wait:
            for thread in self._threads:thread.join()
