# 1. Base image
FROM python:3.9-slim

# 2. Set working directory
WORKDIR /app

# 3. Copy requirements first (for caching)
COPY requirements.txt .

# 4. Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# 5. Copy rest of the project files
COPY . .

# 6. Expose the port that the app will listen on
EXPOSE 7860

# 7. Start the app using a production-grade WSGI server (Gunicorn)
CMD ["gunicorn", "--bind", "0.0.0.0:7860", "app:app"]