# Service Dependency Source Discovery

Deterministically selects files that evidence outbound service dependencies beneath one public GitHub tree URL. It recognises API clients, client registrations, endpoint configuration keys, protobuf services, and construction of cache, database, message-broker (including NServiceBus endpoint configuration), object-storage, and other cloud-service clients; queue, topic and subscription name constants; and platform registrations: a remote configuration store, a telemetry exporter, or an external identity provider. It also always includes entry-point and project files (`Program.cs`, `Startup.cs`, `*.csproj`) and web/job/message-handler shape markers, since those are what `service-dependency-generator` uses to identify a repository's containers. It returns bounded same-repository blob URLs and exclusions; no model is used.

```json
{"sourceUrl":"https://github.com/owner/repository/tree/main/src"}
```

```bash
python3 -m unittest discover -s src/service-dependency-source-discovery -p 'test_*.py'
AZURE_DEV_USER_AGENT=microsoft_foundry_skill azd deploy service-dependency-source-discovery --no-prompt
```
