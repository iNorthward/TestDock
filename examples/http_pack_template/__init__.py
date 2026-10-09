"""Explicit HTTP contract registration; bootstrap never logs in or connects."""
def install():
    from api_client import register_request_auth
    from .integration import ContractHeaders
    register_request_auth(ContractHeaders())
