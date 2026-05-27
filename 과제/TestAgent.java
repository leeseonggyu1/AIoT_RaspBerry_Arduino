import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStreamReader;
import java.io.PrintWriter;
import java.net.InetAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.time.LocalDateTime;
import java.time.format.DateTimeFormatter;
import java.util.concurrent.atomic.AtomicInteger;

public class TestAgent {

    private static final String AGENT_NAME = "TestAgent";
    private static final int PORT = 10010;

    private static final AtomicInteger sendCount = new AtomicInteger(0);

    public static void main(String[] args) {
        System.out.println(AGENT_NAME + " Started");

        try (ServerSocket serverSocket = new ServerSocket(PORT)) {

            while (true) {
                Socket socket = serverSocket.accept();
                System.out.println("Connection Requested.");

                ClientHandler handler = new ClientHandler(socket, serverSocket);
                handler.start();
            }

        } catch (IOException e) {
            System.out.println("Server Error: " + e.getMessage());
            e.printStackTrace();
        }
    }

    static class ClientHandler extends Thread {

        private final Socket socket;
        private final ServerSocket serverSocket;

        public ClientHandler(Socket socket, ServerSocket serverSocket) {
            this.socket = socket;
            this.serverSocket = serverSocket;
        }

        @Override
        public void run() {

            try (
                BufferedReader in = new BufferedReader(
                    new InputStreamReader(socket.getInputStream())
                );

                PrintWriter out = new PrintWriter(
                    socket.getOutputStream(), true
                )
            ) {

                String message;

                while ((message = in.readLine()) != null) {

                    System.out.println("Received : " + message);

                    switch (message) {

                        case "[info]":
                            sendMessage(out, getServerInfo());
                            break;

                        case "[time]":
                            sendMessage(out, getCurrentTime());
                            break;

                        case "[count]":
                            sendMessage(out, String.valueOf(sendCount.get()));
                            break;

                        case "[disconnect]":
                            System.out.println("Client disconnected.");
                            socket.close();
                            return;

                        default:
                            sendMessage(out, "0");
                            break;
                    }
                }

            } catch (IOException e) {
                System.out.println("Client Error: " + e.getMessage());
            }
        }

        private void sendMessage(PrintWriter out, String msg) {
            out.println(msg);
            sendCount.incrementAndGet();
            System.out.println("Sent : " + msg);
        }

        private String getServerInfo() throws IOException {
            InetAddress address = InetAddress.getLocalHost();
            return address.getHostAddress() + ":" + serverSocket.getLocalPort();
        }

        private String getCurrentTime() {
            DateTimeFormatter formatter =
                DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm:ss");

            return LocalDateTime.now().format(formatter);
        }
    }
}
