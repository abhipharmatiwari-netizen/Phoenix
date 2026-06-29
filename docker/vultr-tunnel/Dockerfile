FROM alpine:3.20

RUN apk add --no-cache ca-certificates curl openssh-client

COPY scripts/ops/vultr_reverse_tunnel_entrypoint.sh /usr/local/bin/vultr-reverse-tunnel
RUN chmod 0755 /usr/local/bin/vultr-reverse-tunnel

ENTRYPOINT ["/usr/local/bin/vultr-reverse-tunnel"]
