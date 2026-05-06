# Artifactory Community Edition for C/C++ (cpp-ce)
# Free tier that includes full native Conan repository support.
FROM releases-docker.jfrog.io/jfrog/artifactory-cpp-ce:latest

USER root

RUN mkdir -p /var/opt/jfrog/artifactory/etc/security \
             /var/opt/jfrog/artifactory/data \
             /var/opt/jfrog/artifactory/logs \
             /var/opt/jfrog/artifactory/backup \
    # Generate a master key (required since Derby DB was dropped in 7.x)
    && openssl rand -hex 16 > /var/opt/jfrog/artifactory/etc/security/master.key \
    && chown -R artifactory:artifactory /var/opt/jfrog/artifactory

USER artifactory

VOLUME ["/var/opt/jfrog/artifactory"]

EXPOSE 8081 8082

ENV JF_ROUTER_ENTRYPOINTS_EXTERNALPORT=8082

HEALTHCHECK --interval=30s --timeout=10s --start-period=120s --retries=10 \
    CMD curl -sf http://localhost:8082/artifactory/api/system/ping || exit 1
