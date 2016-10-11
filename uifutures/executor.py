from concurrent.futures import _base
from multiprocessing import connection
import cPickle as pickle
import os
import select
import subprocess
import threading
import time

from .utils import debug
from . import utils
from .future import Future


class HostShutdown(RuntimeError):
    pass

class DependencyFailed(RuntimeError):
    pass


class Executor(_base.Executor):
    
    def __init__(self, max_workers=None):

        self._conn, child_conn = connection.Pipe()

        # Run the host in a brand-new bash. This gives the shell RC files
        # the chance to clean our whatever garbage the calling application
        # (e.g. Maya) has put into the environment. Ideally this would be
        # cleaned up by sitetools.environ, but I haven't had much success
        # with doing that at Mark Media.
        # TODO: Go back to calling python directly when that is fixed.
        cmd = ['bash', '-lc', 'python -m uifutures.host %s' % child_conn.fileno()]
        env = None
        self.proc = subprocess.Popen(cmd, env=env)
        
        child_conn.close()
        
        # Later, we may need to wait on the handshake to make sure that the
        # process has started. But since we know that the socket is open since
        # it is an OS pipe, we don't have to wait.
        
        # Send some configuration over.
        if max_workers:
            self._conn.send(dict(
                type='config',
                max_workers=max_workers,
            ))
        
        self._futures = {}
        
        self._host_alive = True
        self._host_listener_thread = threading.Thread(target=self._host_listener)
        self._host_listener_thread.daemon = True
        self._host_listener_thread.start()
    
    def shutdown(self, wait=True):
        self._conn.send(dict(
            type='shutdown',
        ))
    
    def _host_listener(self):
        try:
            while self._host_alive:
                try:
                    rlist, _, _ = select.select([self._conn], [], [])
                    msg = self._conn.recv()
                    type_ = msg.pop('type', None)
                    # debug('Executor: new message of type %r:\n%s', type_, pprint.pformat(msg))
                    handler = getattr(self, '_do_' + (type_ or 'missing'), None)
                    if not handler:
                        debug('Executor: no handler for %r', type_)
                        continue
                    handler(**msg)
                except IOError as e:
                    if e.errno == 35:
                        debug('Executor: socket temporarily unavailable; sleeping')
                        time.sleep(0.25)
                    else:
                        raise
        except EOFError:
            pass
            # debug('Executor: EOF')
        finally:
            self._do_shutdown()
    
    def _do_handshake(self, pid):
        pass
    
    def _do_shutdown(self):
        self._host_alive = False
        # debug('Executor: host shutdown')
        for future in self._futures.itervalues():
            future.set_exception(HostShutdown('host shutdown'))
    
    def _do_result(self, uuid, **msg):
        # debug('Executor: %s finished', uuid)
        future = self._futures.pop(uuid)
        
        result = (pickle.loads(msg['package']) if 'package' in msg else msg)['result']
        future.set_result(result)
        
    def _do_exception(self, uuid, **msg):
        # debug('Executor: %s errored', uuid)
        future = self._futures.pop(uuid)
        exception = (pickle.loads(msg['package']) if 'package' in msg else msg)['exception']
        future.set_exception(exception)
    
    def submit(self, func, *args, **kwargs):
        self.submit_ext(func, args, kwargs)
    
    def submit_ext(self, func, args=None, kwargs=None, name=None, icon=None, depends_on=None):
        
        uuid = os.urandom(16).encode('hex')
        func_name = utils.get_func_name(func)
        
        depends_on = depends_on or []
        if not isinstance(depends_on, (list, tuple)):
            depends_on = [depends_on]
        depends_on = [x.uuid for x in depends_on]
        
        self._conn.send(dict(
            type='submit',
            uuid=uuid,
            name=name or func_name,
            icon=icon,
            func_name=func_name,
            depends_on=depends_on,
            package=pickle.dumps(dict(
                func=func,
                args=tuple(args or ()),
                kwargs=dict(kwargs or {}),
            ), protocol=-1),
        ))
        
        future = Future(uuid)
        self._futures[uuid] = future
        return future
        
        

