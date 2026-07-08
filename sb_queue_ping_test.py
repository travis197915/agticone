"""
Simple Azure Service Bus queue connectivity ping test.

Usage:
1) Paste your connection string and queue name below.
2) Install dependency: pip install azure-servicebus
3) Run: python sb_queue_ping_test.py
"""

import socket
from azure.servicebus import ServiceBusClient, TransportType
from azure.servicebus.exceptions import ServiceBusError
from azure.servicebus.management import ServiceBusAdministrationClient

# Paste your values here
CONNECTION_STRING = "Endpoint=sb://bh-agenticai-sb-dev.servicebus.windows.net/;SharedAccessKeyName=RootManageSharedAccessKey;SharedAccessKey=ApFDqBvHDGgI9jcUYkOZJyOaGINfgu8KZ+ASbPU3Ii4="
QUEUE_NAME = "orders-sb-queue"


def check_tcp(host: str, port: int, timeout_seconds: int = 3) -> bool:
    """Returns True if a TCP socket can be opened to host:port."""
    try:
        with socket.create_connection((host, port), timeout=timeout_seconds):
            return True
    except Exception:
        return False


def get_namespace_host_from_connection_string(connection_string: str) -> str:
    """Extracts host from 'Endpoint=sb://<host>/' in the connection string."""
    for part in connection_string.split(";"):
        if part.startswith("Endpoint=sb://"):
            endpoint = part.replace("Endpoint=sb://", "").strip()
            return endpoint.rstrip("/")
    return ""


def ping_service_bus_queue(connection_string: str, queue_name: str) -> bool:
    """Returns True if queue connectivity/auth works via WebSocket, else False."""
    if not connection_string or not queue_name:
        print("Connection failed: CONNECTION_STRING or QUEUE_NAME is empty")
        return False

    namespace_host = get_namespace_host_from_connection_string(connection_string)
    if namespace_host and not check_tcp(namespace_host, 443):
        print(f"Connection failed: {namespace_host}:443 is blocked/unreachable")
        return False

    # Check queue metadata first so we can report a clear queue/auth issue.
    try:
        admin_client = ServiceBusAdministrationClient.from_connection_string(connection_string)
        admin_client.get_queue(queue_name)
    except Exception as err:
        print(f"Connection failed: queue validation failed for '{queue_name}': {err}")
        print("Hint: verify queue name and that the SAS policy has Manage or Listen rights.")
        return False

    try:
        with ServiceBusClient.from_connection_string(
            connection_string,
            transport_type=TransportType.AmqpOverWebsocket,
        ) as client:
            with client.get_queue_receiver(queue_name=queue_name, max_wait_time=8) as receiver:
                # Forces a real service call for auth/network validation.
                receiver.receive_messages(max_message_count=1, max_wait_time=8)

        print("Connection successful")
        return True

    except ServiceBusError as err:
        print(f"Connection failed: {err}")
        print("Hint: this is usually queue permissions, queue name, or namespace firewall/private endpoint rules.")
        return False
    except Exception as err:
        print(f"Connection failed: {err}")
        return False


if __name__ == "__main__":
    ping_service_bus_queue(CONNECTION_STRING, QUEUE_NAME)
