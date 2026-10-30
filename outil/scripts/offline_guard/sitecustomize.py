"""Garde réseau de recette ; compatibilité du lancement -c/-m sous Windows."""
import sys
import __main__
import ipaddress
import os

# L'environnement Windows instrumenté consulte __main__.__file__ lors d'un
# événement d'audit, avant que -c/-m ne l'initialise. Aucun import réseau préalable.
if not hasattr(__main__, "__file__"):
    __main__.__file__ = "<dv-offline-python-startup>"
sys.dont_write_bytecode = True


ALLOW_LOOPBACK = os.environ.get("DV_AUDIT_ALLOW_LOOPBACK") == "1"


def _literal_loopback(address):
    host = address[0] if isinstance(address, tuple) else address
    try:
        return ipaddress.ip_address(host).is_loopback
    except (ValueError, TypeError):
        return False


def no_network(event, args):
    if event in {"socket.connect", "socket.connect_ex", "socket.getaddrinfo",
                 "socket.gethostbyname", "socket.gethostbyaddr", "socket.sendto", "socket.bind"}:
        address = args[1] if event in {"socket.connect", "socket.connect_ex", "socket.sendto", "socket.bind"} else args[0]
        if ALLOW_LOOPBACK and _literal_loopback(address):
            return
        raise RuntimeError("AUDIT_REAL_NETWORK_DISABLED:" + event)


sys.addaudithook(no_network)
