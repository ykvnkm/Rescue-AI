from fastapi import FastAPI

app = FastAPI(title="API")

@app.get("/health")
def health():
    return {"status": "ok"}
