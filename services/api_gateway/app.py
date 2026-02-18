from fastapi import FastAPI

app = FastAPI(title="API")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/ready")
def ready():
    return {"status": "ready"}


@app.get("/version")
def version():
    return {"version": "0.1.0"}
