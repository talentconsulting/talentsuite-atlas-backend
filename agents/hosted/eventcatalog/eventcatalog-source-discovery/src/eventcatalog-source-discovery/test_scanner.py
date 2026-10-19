import io
import unittest
import zipfile

from scanner import scan


SOURCE = "https://github.com/source/catalog/tree/main/src/Application"


def archive(files):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as output:
        for path, content in files.items():
            output.writestr(f"catalog-main/{path}", content)
    return buffer.getvalue()


class ScanTests(unittest.TestCase):
    def test_selects_messages_and_handlers_and_reports_oversized_files(self):
        result = scan(SOURCE, archive({
            "src/Application/Commands/CreateOrderCommand.cs": "record CreateOrderCommand(int Id) : IRequest;",
            "src/Application/Events/OrderCreatedEvent.cs": "record OrderCreatedEvent(int Id) : INotification;",
            "src/Application/Handlers/CreateOrderHandler.cs": "class CreateOrderHandler : IRequestHandler<CreateOrderCommand> {}",
            "src/Application/Commands/large.cs": "x" * (512 * 1024 + 1),
            "src/Application/AdhocScripts/Manual/Ignore.cs": "record IgnoredEvent() : INotification;",
        }))

        self.assertEqual(3, len(result["sourceFiles"]))
        self.assertTrue(result["sourceFiles"][0].endswith("Commands/CreateOrderCommand.cs"))
        self.assertEqual(
            [{"path": "src/Application/Commands/large.cs", "reason": "file_too_large"}],
            result["excludedFiles"],
        )

    def test_never_selects_files_outside_the_requested_tree(self):
        result = scan(SOURCE, archive({
            "src/Application/Commands/CreateOrderCommand.cs": "record CreateOrderCommand() : IRequest;",
            "src/Other/Events/OutsideEvent.cs": "record OutsideEvent() : INotification;",
        }))

        self.assertEqual(
            ["https://github.com/source/catalog/blob/main/src/Application/Commands/CreateOrderCommand.cs"],
            result["sourceFiles"],
        )


class SendingFileTests(unittest.TestCase):
    def test_files_that_send_messages_are_selected_first(self):
        from scanner import _message_source, _priority
        routing = "using SFA.DAS.Notifications.Messages.Commands;\nclass R { void A() { routing.RouteToEndpoint(typeof(SendEmailCommand), \"x\"); } }"
        sender = "using SFA.DAS.Notifications.Messages.Commands;\nclass S { Task A() => _session.Send(new SendEmailCommand()); }"
        mediatr_only = "using MediatR;\nclass M { Task A() => _mediator.Send(new GetThingQuery()); }"
        self.assertTrue(_message_source("src/Extensions/RoutingSettingsExtensions.cs", routing))
        self.assertEqual(-1, _priority("src/Extensions/RoutingSettingsExtensions.cs", routing)[0])
        self.assertEqual(-1, _priority("src/Services/Sender.cs", sender)[0])
        self.assertGreater(_priority("src/Services/Query.cs", mediatr_only)[0], -1)
