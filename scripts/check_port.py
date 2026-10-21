import socket
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
try:
    s.settimeout(2)
    s.connect(('localhost', 8000))
    print('OPEN')
except Exception as e:
    print('CLOSED', type(e).__name__, str(e))
finally:
    s.close()
