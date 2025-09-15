# Stage 1: Use an official Python runtime as a parent image
# Using a "slim" version keeps the final image size smaller.
FROM python:3.11-slim

# Stage 2: Set the working directory inside the container
WORKDIR /app

# Stage 3: Install dependencies
# We copy ONLY the requirements file first to leverage Docker's layer caching.
# This way, Docker doesn't need to reinstall everything unless this file changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Stage 4: Copy the rest of your application code into the container
# This includes your 'run.py' file and the entire 'app' folder.
COPY . .

# Stage 5: Expose the port the app will run on
EXPOSE 5000

# Stage 6: Define the command to run the application
# We use Gunicorn as our production server. It will look for the 'app' object
# inside the 'run.py' file (which is referenced as 'run').
# '--bind 0.0.0.0:5000' is essential for Docker to expose the app correctly.
CMD ["gunicorn", "--workers", "3", "--preload", "--bind", "0.0.0.0:5000", "run:app"]