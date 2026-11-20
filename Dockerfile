# Builds the agent-baton CLI image. There is no infrastructure-as-code in this
# repository (see README "What this needs to run") - this image needs a real
# DynamoDB table and real AWS credentials at run time to do anything useful;
# building and running it is a caller's own choice of where (Lambda, EKS, EC2).
#
# Build and run on the project's dedicated test runner, never on a
# contributor's laptop (see README "Testing").
FROM python:3.11.9-slim-bookworm AS build

WORKDIR /build
COPY pyproject.toml README.md CONTRACT.md ./
COPY agent_baton ./agent_baton
RUN pip install --no-cache-dir --prefix=/install .

FROM python:3.11.9-slim-bookworm

COPY --from=build /install /usr/local

RUN useradd --create-home --shell /usr/sbin/nologin agent-baton
USER agent-baton

ENTRYPOINT ["agent-baton"]
CMD ["--help"]
