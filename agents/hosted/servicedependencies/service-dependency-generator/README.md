# Service Dependency Generator

Reads a bounded set of source files chosen by `service-dependency-source-discovery` and returns a validated catalog of containers and their outbound dependencies: HTTP APIs, gRPC services, caches, databases, message brokers (with the queues, topics and subscriptions each one sends to or receives from), object storage, other cloud services, and the platform services nearly every service has (`configuration-store`, `telemetry` and `identity-provider` kinds). It records configuration keys and source evidence but never returns secret values.

The model first identifies `containers`: the independently-deployable projects evidenced in the bundle (an API, a web app, a background job, a message handler). The primary/API container is named after the system itself (never suffixed "Api"); any other container is named `"<system> <role>"` (for example "Commitments Web"). Each kept dependency is a C4-style relationship from the container that evidences it to the dependency: `sourceId` references a container id, `targetId` is a stable slug of the dependency's kind and name (shared across containers that depend on the same thing), and `description` is a deterministic action verb derived from `kind` (for example "Reads from and writes to" for a database, "Calls the API of" for an HTTP API). A dependency's `name` is always its shortest business name -- a leading `I` interface prefix and generic role suffixes such as `Client`, `ApiClient`, `Api`, `Service`, or `Proxy` are stripped even when only one local name is evidenced (`IApprenticeshipLevyApiClient` becomes "Apprenticeship Levy"). The model is also asked to collapse the same real dependency evidenced under different local names (an interface, an implementation class, a friendlier name) into one entry, using that same cleaned name, rather than reporting it more than once.

```json
{"sourceUrl":"https://github.com/owner/repository/tree/main/src","sourceFiles":["https://github.com/owner/repository/blob/main/src/Clients/AccountsClient.cs"]}
```

```bash
python3 -m unittest discover -s src/service-dependency-generator -p 'test_*.py'
AZURE_DEV_USER_AGENT=microsoft_foundry_skill azd deploy service-dependency-generator --no-prompt
```
