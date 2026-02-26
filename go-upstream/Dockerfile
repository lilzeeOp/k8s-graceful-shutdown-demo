# Build stage
FROM golang:1.22-alpine AS builder

RUN apk add --no-cache git

WORKDIR /app

COPY go.mod ./
RUN go mod download

COPY . .
RUN go mod tidy && CGO_ENABLED=0 GOOS=linux go build -o app .

# Run stage
FROM alpine:3.19

WORKDIR /app
COPY --from=builder /app/app .

EXPOSE 7000

# Run binary directly (no shell wrapper) so it receives SIGTERM properly
CMD ["./app"]
