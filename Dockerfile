FROM mcr.microsoft.com/playwright/python:v1.49.0-jammy
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY swiggy_kalyan_tracker.py .
ENV USE_BROWSER=1 PYTHONUNBUFFERED=1
CMD ["python", "swiggy_kalyan_tracker.py"]
