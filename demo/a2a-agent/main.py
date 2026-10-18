import uvicorn

from app import create_app

if __name__ == "__main__":
    # Built here (not at import) so a missing QUIZ_SIGNING_KEY or bad model settings stop the process at startup.
    uvicorn.run(create_app(), host="0.0.0.0", port=8080)
