import socket
import bluetooth

BT_ADDRESS = "98:DA:60:11:16:B0"   # Change to bluetooth device address
BT_PORT = 1

SOCK_HOST = "192.168.0.5"           # Change to server IP address
SOCK_PORT = 10020

BUFFER_SIZE = 1024
ENCODING = "utf-8"


def bt_receive_msg(bt_sock):
    buffer = ""

    try:
        while True:
            data = bt_sock.recv(BUFFER_SIZE)

            if not data:
                print("Bluetooth connection closed.")
                return None

            text = data.decode(ENCODING, errors="replace")
            buffer += text

            if "\n" in buffer:
                message, buffer = buffer.split("\n", 1)
                message = message.strip()
                print("Received data from bluetooth device:", message)
                return message

    except Exception as e:
        print("Failed to receive message by bluetooth:", e)
        return None


def bt_send_msg(bt_sock, message):
    try:
        if not message.endswith("\n"):
            message += "\n"

        bt_sock.send(message.encode(ENCODING))
        print("Sent data to bluetooth device:", message.strip())

    except Exception as e:
        print("Failed to send message to bluetooth device:", e)


def client_program():
    print("Searching nearby bluetooth devices...")
    nearby_devices = bluetooth.discover_devices(
        duration=8,
        lookup_names=True,
        flush_cache=True
    )

    found_addresses = [addr for addr, name in nearby_devices]

    if BT_ADDRESS not in found_addresses:
        print("Cannot search target bluetooth device.")
        return

    print("Target bluetooth device searched successfully.")

    bt_sock = bluetooth.BluetoothSocket(bluetooth.RFCOMM)
    bt_sock.connect((BT_ADDRESS, BT_PORT))
    print("Bluetooth connection succeeded.")

    try:
        while True:
            print("Ready to receive message from bluetooth device...")
            message = bt_receive_msg(bt_sock)

            if message is None:
                print("No valid bluetooth message received.")
                break

            # Echo received message back to bluetooth device
            bt_send_msg(bt_sock, message)
            print()

            print(f"Attempting to connect to the server... {SOCK_HOST}:{SOCK_PORT}")
            client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

            try:
                client_socket.connect((SOCK_HOST, SOCK_PORT))
                print("Connection established.")

                # Server readLine() needs \n
                send_message = message + "\n"
                client_socket.sendall(send_message.encode(ENCODING))
                print("Sent to server:", message)

                response = client_socket.recv(BUFFER_SIZE).decode(ENCODING, errors="replace")
                print("Received from server:", response.strip())

                client_socket.sendall("[disconnect]\n".encode(ENCODING))
                print("Disconnected from the server.")

            finally:
                client_socket.close()

            print()

    except Exception as e:
        print("An error occurred:", e)

    finally:
        bt_sock.close()
        print("Bluetooth socket closed.")


if __name__ == "__main__":
    client_program()
