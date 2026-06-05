FROM golang:1.26-alpine AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 go build -o /mock-bitcoind ./dev/mock-bitcoind

FROM alpine:3.21
COPY --from=build /mock-bitcoind /mock-bitcoind
EXPOSE 8332
CMD ["/mock-bitcoind"]
