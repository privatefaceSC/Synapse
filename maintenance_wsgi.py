"""Temporary read-only maintenance endpoint for database cutover."""


def application(_environ, start_response):
    body = ("Synapse временно недоступен: перенос базы данных. "
            "Повторите попытку позже.\n").encode("utf-8")
    start_response("503 Service Unavailable", [
        ("Content-Type", "text/plain; charset=utf-8"),
        ("Content-Length", str(len(body))),
        ("Retry-After", "600"),
        ("Cache-Control", "no-store"),
    ])
    return [body]


app = application
