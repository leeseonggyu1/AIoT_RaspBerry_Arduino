import bluetooth

TARGET_ADDRESS = "98:DA:60:11:16:B0"
PORT = 1
BUFFER_SIZE = 1024
ENCODING = "utf-8"


def receive_message(sock):
    buffer = ""

    try:
        while True:
            data = sock.recv(BUFFER_SIZE)

            if not data:
                raise ConnectionError("Connection closed by remote device.")

            text = data.decode(ENCODING, errors="replace")
            buffer += text

            if "\n" in buffer:
                message, buffer = buffer.split("\n", 1)
                message = message.strip()
                print("Received:", message)
                return message

    except Exception as e:
        print("Failed to receive message:", e)
        return None


def send_message(sock, message):
    try:
        if not message.endswith("\n"):
            message += "\n"

        sock.send(message.encode(ENCODING))
        print("Sent:", message.strip())

    except Exception as e:
        print("Failed to send message:", e)


def main():
    try:
        print("Searching nearby Bluetooth devices...")
        nearby_devices = bluetooth.discover_devices(
            duration=8,
            lookup_names=True,
            flush_cache=True
        )

        found_addresses = [addr for addr, name in nearby_devices]

        if TARGET_ADDRESS not in found_addresses:
            print("Cannot find target device.")
            return

        print("Target device found.")

        sock = bluetooth.BluetoothSocket(bluetooth.RFCOMM)
        sock.connect((TARGET_ADDRESS, PORT))
        print("Bluetooth connection succeeded.")

        try:
            while True:
                print("Ready to receive...")
                message = receive_message(sock)

                if message is None:
                    print("No valid message received. Closing connection.")
                    break

                # Echo received message back
                send_message(sock, message)

        finally:
            sock.close()
            print("Socket closed.")

    except bluetooth.BluetoothError as e:
        print("Bluetooth error:", e)
    except Exception as e:
        print("Error:", e)


if __name__ == "__main__":
    main()
