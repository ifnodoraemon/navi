FROM python:3.13

WORKDIR /app

# Install build dependencies
RUN pip install --no-cache-dir --upgrade pip setuptools wheel

# Copy project files necessary for build
COPY pyproject.toml README.md ./
COPY src ./src

# Install the package and its dependencies
RUN pip install --no-cache-dir .

# Run the navi CLI entrypoint
ENTRYPOINT ["navi"]
