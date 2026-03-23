# Use the official lightweight Python 3.11 image
FROM python:3.11-slim
 
# Set the working directory inside the container
WORKDIR /app
 
# Install dependencies first (cached as a separate layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
 
# Copy the rest of the application code
COPY . .
 
# Run the app with gunicorn on the port Cloud Run injects via $PORT
CMD exec gunicorn --bind :$PORT --workers 1 --threads 8 --timeout 300 main:app