from lastbridge.app import create_app
from lastbridge.bootstrap import Providers


def make_app(settings):
    # Imported on invocation to avoid a fixture/fake import cycle.
    from support.audio import FakeAudio
    from support.catalog import FakeProvider

    return create_app(
        settings,
        start_workers=False,
        providers=Providers(metadata=FakeProvider(), audio=FakeAudio()),
    )
