import socket
import time
import random


def client_program():
    host = "192.168.0.5"
    port = 10020

    while True:
        try:
            print(f"Attempting to connect to the server... {host}:{port}")

            client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            client_socket.connect((host, port))
            print("Connection established.")

            number = random.randint(1, 100)
            message = str(number) + "\n"
            client_socket.sendall(message.encode("utf-8"))
            print(f"Sent: {message.strip()}")

            response = client_socket.recv(1024).decode("utf-8")
            print(f"Received: {response.strip()}")

            client_socket.sendall("[disconnect]\n".encode("utf-8"))
            client_socket.close()
            print("Disconnected from the server.")

            time.sleep(3)

        except Exception as e:
            print(f"An error occurred: {e}")
            break


if __name__ == "__main__":
    client_program()
