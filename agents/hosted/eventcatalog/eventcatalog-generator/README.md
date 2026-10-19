# Event and Command Catalog Generator

Model-backed hosted agent that reads the bounded `sourceFiles` selected by discovery and returns one strictly validated event and command catalog. Source text is treated as untrusted data and the generator may only include messages, fields, and handlers evidenced by code.

## Input

```json
{
  "sourceUrl": "https://github.com/owner/repository/tree/main/src/Application",
  "sourceFiles": [
    "https://github.com/owner/repository/blob/main/src/Application/Commands/CreateOrderCommand.cs"
  ]
}
```

## Output

```json
{
  "repository": "owner/repository",
  "ref": "main",
  "path": "src/Application",
  "commands": [
    {
      "name": "CreateOrderCommand",
      "namespace": "Application.Commands",
      "sourceFile": "src/Application/Commands/CreateOrderCommand.cs",
      "description": null,
      "fields": [
        {"name": "orderId", "type": "Guid", "required": true, "description": null}
      ],
      "handlers": [
        {"name": "CreateOrderHandler", "sourceFile": "src/Application/Handlers/CreateOrderHandler.cs"}
      ]
    }
  ],
  "events": []
}
```

Limits are 100 selected files, 512 KiB per file, and 2 MiB combined.

```bash
python3 -m unittest discover -s src/eventcatalog-generator -p 'test_*.py'
AZURE_DEV_USER_AGENT=microsoft_foundry_skill azd deploy eventcatalog-generator --no-prompt
```

Each message also lists its `senders` (`name`, `sourceFile`): the classes that send, publish or route it. Messages owned by other services that the repository sends (for example `SFA.DAS.Notifications.Messages.Commands.SendEmailCommand`) are included with their senders and empty `handlers`. Besides the model, a deterministic pass records every send it can see in files that import a `...Messages...` namespace (`RouteToEndpoint(typeof(X))`, `Send<X>`/`Send(new X(...))`, `X x = new(...)` then sent; MediatR excluded), so these are not missed.
