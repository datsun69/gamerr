# Stage 1: Use an official Python runtime as a parent image
FROM python:3.11-slim

# Stage 2: Set the working directory inside the container
WORKDIR /app

# Stage 3: Install dependencies
# --- THIS IS THE FINAL, CORRECT FIX ---
# Modern Debian images use a new sources format. We edit the 'debian.sources' file
# to add 'contrib' and 'non-free' components.
RUN sed -i 's/Components: main/Components: main contrib non-free/g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y unrar \
    && rm -rf /var/lib/apt/lists/*
# --- END OF FIX ---

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Stage 4: Copy the rest of your application code into the container
COPY . .

# Stage 5: Expose the port the app will run on
EXPOSE 5000

# Stage 6: Define the command to run the application
CMD ["gunicorn", "--workers", "3", "--preload", "--bind", "0.0.0.0:5000", "run:app"]