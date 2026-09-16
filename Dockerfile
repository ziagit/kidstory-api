# kidstory-api as a Hugging Face Docker Space (also runs anywhere else that
# sets $PORT — Cloud Run, Railway, Render). Spaces route traffic to 7860 and
# run the container as uid 1000; both are honoured below.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=7860

WORKDIR /code

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app

RUN useradd -m -u 1000 user
USER user

EXPOSE 7860
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
