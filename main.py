import json
import socket
import logging
import sqlite3
from threading import Thread, Lock
import time



class ThreadSafeDB:
    def __init__(self, db_path):
        self.db_path = db_path
        self.lock = Lock()
        self._init_db()

    def _init_db(self):
        with self.lock:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL,
                password TEXT NOT NULL,
                message TEXT NOT NULL
            )
            ''')
            conn.commit()
            conn.close()

    def execute(self, query, args=(), fetch=False):
        with self.lock:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            try:
                cursor.execute(query, args)
                if fetch:
                    result = cursor.fetchall()
                else:
                    result = cursor.lastrowid
                conn.commit()
                return result
            finally:
                conn.close()


class HTTPServer:
    def __init__(self, host: str, port: int, server_name: str):
        self._host = host
        self._port = port
        self._server_name = server_name
        self.db = ThreadSafeDB("database.db")
        logging.basicConfig(level=logging.INFO, filename="server.log", filemode="w")
        logging.info(f"Server '{server_name}' initialized on {host}:{port}")

    def start_server(self):
        serv_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM, proto=0)
        serv_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        serv_sock.bind((self._host, self._port))
        serv_sock.listen()
        logging.info(f'Server started on http://{self._host}:{self._port}')

        try:
            while True:
                conn, addr = serv_sock.accept()
                logging.info(f"Connection from {addr}")
                Thread(target=self.handle_client, args=(conn,)).start()
        except KeyboardInterrupt:
            logging.warning('Server shutting down...')
        finally:
            serv_sock.close()

    def handle_client(self, conn: socket.socket):
        try:
            raw_request = conn.recv(8192)
            if not raw_request:
                return

            request_text = raw_request.decode('utf-8')
            parts = request_text.split('\r\n\r\n', 1)
            headers = parts[0]
            body = parts[1] if len(parts) > 1 else None

            first_line = headers.split('\r\n')[0]
            method, path, _ = first_line.split()

            headers_dict = {}
            for header in headers.split('\r\n')[1:]:
                if ': ' in header:
                    key, value = header.split(': ', 1)
                    headers_dict[key] = value

            if '?' in path:
                path, params_str = path.split('?', 1)
                parameters = self._parse_parameters(params_str)
            else:
                parameters = {}

            if path == '/about':
                self._about(conn)
            elif path == '/users':
                self._handle_users(conn, method, parameters, body)
            else:
                self._send_response(conn, 404, {'error': 'Path not found'})

        except Exception as e:
            logging.error(f"Client handling failed: {e}")
            self._send_response(conn, 500, {'error': 'Internal server error'})
        finally:
            conn.close()

    def _handle_users(self, conn, method, parameters, body):
        try:
            if method == 'GET':
                self._get_users(conn, parameters)
            elif method == 'POST':
                self._create_user(conn, body)
            elif method == 'DELETE':
                self._delete_user(conn, parameters)
            elif method == 'PATCH':
                self._update_user(conn, parameters, body)
            else:
                self._send_response(conn, 405, {'error': 'Method not allowed'})
        except Exception as e:
            logging.error(f"Users endpoint error: {e}")
            self._send_response(conn, 400, {'error': str(e)})

    def _get_users(self, conn, parameters):
        user_id = parameters.get('id')

        if user_id:
            result = self.db.execute(
                "SELECT id, email, message FROM users WHERE id = ?",
                (user_id,),
                fetch=True
            )
            if result:
                user = result[0]
                response = {
                    'id': user[0],
                    'email': user[1],
                    'message': user[2]
                }
                self._send_response(conn, 200, response)
            else:
                self._send_response(conn, 404, {'error': 'User not found'})
        else:
            users = self.db.execute(
                "SELECT id, email, message FROM users",
                fetch=True
            )
            response = {
                'count': len(users),
                'users': [
                    {
                        'id': user[0],
                        'email': user[1],
                        'message': user[2]
                    } for user in users
                ]
            }
            self._send_response(conn, 200, response)

    def _create_user(self, conn, body):
        if not body:
            raise ValueError("Request body is empty")

        try:
            data = json.loads(body)
            if not all(key in data for key in ['email', 'password', 'message']):
                raise ValueError("Missing required fields: email, password, message")

            user_id = self.db.execute(
                "INSERT INTO users (email, password, message) VALUES (?, ?, ?)",
                (data['email'], data['password'], data['message'])
            )

            self._send_response(conn, 201, {
                'message': 'User created successfully',
                'user_id': user_id
            })
            logging.info(f"Created new user with id {user_id}")

        except json.JSONDecodeError:
            raise ValueError("Invalid JSON format")

    def _delete_user(self, conn, parameters):
        user_id = parameters.get('id')
        if not user_id:
            raise ValueError("Missing user id parameter")

        self.db.execute("DELETE FROM users WHERE id = ?", (user_id,))
        self._send_response(conn, 200, {'message': f'User {user_id} deleted'})
        logging.info(f"Deleted user with id {user_id}")

    def _update_user(self, conn, parameters, body):
        user_id = parameters.get('id')
        if not user_id:
            raise ValueError("Missing user id parameter")

        if not body:
            raise ValueError("Request body is empty")

        try:
            data = json.loads(body)
            updates = []
            params = []

            if 'email' in data:
                updates.append("email = ?")
                params.append(data['email'])
            if 'password' in data:
                updates.append("password = ?")
                params.append(data['password'])
            if 'message' in data:
                updates.append("message = ?")
                params.append(data['message'])

            if not updates:
                raise ValueError("No fields to update")

            params.append(user_id)
            query = f"UPDATE users SET {', '.join(updates)} WHERE id = ?"

            self.db.execute(query, params)
            self._send_response(conn, 200, {'message': f'User {user_id} updated'})
            logging.info(f"Updated user with id {user_id}")

        except json.JSONDecodeError:
            raise ValueError("Invalid JSON format")

    def _about(self, conn):
        about_info = {
            "server_name": self._server_name,
            "version": "1.0",
            "supported_paths": {
                "/about": "Server information",
                "/users": "User management endpoint"
            },
            "supported_methods": ["GET", "POST", "DELETE", "PATCH"],
            "start_time": time.strftime("%Y-%m-%d %H:%M:%S")
        }

        # Генерация HTML-страницы
        html_content = f"""
        <!DOCTYPE html>
        <html lang="en">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>About {about_info['server_name']}</title>
            <style>
                body {{
                    font-family: Arial, sans-serif;
                    line-height: 1.6;
                    margin: 0;
                    padding: 20px;
                    color: #333;
                }}
                .container {{
                    max-width: 800px;
                    margin: 0 auto;
                }}
                h1 {{
                    color: #2c3e50;
                    border-bottom: 2px solid #3498db;
                    padding-bottom: 10px;
                }}
                .info-section {{
                    margin-bottom: 20px;
                    background: #f9f9f9;
                    padding: 15px;
                    border-radius: 5px;
                }}
                .info-section h2 {{
                    margin-top: 0;
                    color: #3498db;
                }}
                ul {{
                    list-style-type: none;
                    padding: 0;
                }}
                li {{
                    margin-bottom: 5px;
                }}
                .path {{
                    font-weight: bold;
                    color: #e74c3c;
                }}
                .method {{
                    display: inline-block;
                    padding: 2px 6px;
                    background: #3498db;
                    color: white;
                    border-radius: 3px;
                    margin-right: 5px;
                    font-size: 0.8em;
                }}
            </style>
        </head>
        <body>
            <div class="container">
                <h1>{about_info['server_name']}</h1>

                <div class="info-section">
                    <h2>Server Information</h2>
                    <p><strong>Version:</strong> {about_info['version']}</p>
                    <p><strong>Started at:</strong> {about_info['start_time']}</p>
                </div>

                <div class="info-section">
                    <h2>Supported Paths</h2>
                    <ul>
                        {"".join(f'<li><span class="path">{path}</span>: {description}</li>'
                                 for path, description in about_info['supported_paths'].items())}
                    </ul>
                </div>

                <div class="info-section">
                    <h2>Supported Methods</h2>
                    <ul>
                        {"".join(f'<li><span class="method">{method}</span></li>'
                                 for method in about_info['supported_methods'])}
                    </ul>
                </div>
            </div>
        </body>
        </html>
        """

        # Отправка HTML-ответа
        headers = (
            f"HTTP/1.1 200 OK\r\n"
            f"Content-Type: text/html\r\n"
            f"Content-Length: {len(html_content)}\r\n"
            f"\r\n"
        )

        conn.sendall(headers.encode('utf-8') + html_content.encode('utf-8'))

    @staticmethod
    def _parse_parameters(params_str: str) -> dict:
        return dict(param.split('=') for param in params_str.split('&') if '=' in param)

    @staticmethod
    def _send_response(conn, status_code, content):
        response = {
            'status_code': status_code,
            'data': content
        }
        json_response = json.dumps(response)

        headers = (
            f"HTTP/1.1 {status_code}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(json_response)}\r\n"
            f"\r\n"
        )

        conn.sendall(headers.encode('utf-8') + json_response.encode('utf-8'))


if __name__ == "__main__":
    host = '127.0.0.1'
    port = 8080
    server_name = 'User Management Server'

    server = HTTPServer(host, port, server_name)
    server.start_server()