from fastapi import FastAPI

from services.api_gateway.presentation.http.routes import router

app = FastAPI(title="API")
app.include_router(router)
