ARG BASE_IMAGE
FROM "${BASE_IMAGE}"

ARG DAGSTER_VERSION

COPY build_cache/ /

RUN pip install \
    -e dagster \
    -e dagster-pipes \
    -e dagster-shared \
    -e dagster-postgres \
    -e dagster-celery[flower,redis,kubernetes] \
    -e dagster-aws \
    -e dagster-k8s \
    -e dagster-celery-k8s

COPY example_project/ /
