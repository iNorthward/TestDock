"""Explicit login callback and expiry-aware in-memory TokenProvider recipe."""
import math,threading,time
class CachedLoginTokens:
    def __init__(self,login,*,max_ttl,refresh_margin=10,clock=time.monotonic):
        if type(max_ttl) not in (int,float) or not math.isfinite(max_ttl) or max_ttl<=0:raise ValueError('finite positive max_ttl required')
        if type(refresh_margin) not in (int,float) or not math.isfinite(refresh_margin) or not 0<=refresh_margin<max_ttl:raise ValueError('invalid refresh margin')
        self.login=login;self.max_ttl=max_ttl;self.margin=refresh_margin;self.clock=clock;self._cache={};self._lock=threading.Lock()
    def get_token(self,username,tenant_id):
        key=(str(username),str(tenant_id))
        with self._lock:
            now=self.clock();cached=self._cache.get(key)
            if cached and cached[1]>now:return cached[0]
            result=self.login(username,tenant_id)
            if not isinstance(result,dict) or not isinstance(result.get('access_token'),str) or not result['access_token'].strip():raise ValueError('login returned no valid token')
            ttl=result.get('expires_in')
            if type(ttl) not in (int,float) or not math.isfinite(ttl) or ttl<=0:raise ValueError('login requires explicit finite positive expires_in')
            deadline=self.clock()+max(0,min(ttl,self.max_ttl)-self.margin)
            self._cache[key]=(result['access_token'],deadline)
            return result['access_token']
    def invalidate(self,username,tenant_id):
        with self._lock:self._cache.pop((str(username),str(tenant_id)),None)
