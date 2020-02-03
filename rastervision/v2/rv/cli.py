import click

from rastervision.v2.core.main import main
from rastervision.v2.core import _rv_config
from rastervision.v2.rv.predictor import Predictor


# https://stackoverflow.com/questions/48391777/nargs-equivalent-for-options-in-click
class OptionEatAll(click.Option):
    def __init__(self, *args, **kwargs):
        self.save_other_options = kwargs.pop('save_other_options', True)
        nargs = kwargs.pop('nargs', -1)
        assert nargs == -1, 'nargs, if set, must be -1 not {}'.format(nargs)
        super(OptionEatAll, self).__init__(*args, **kwargs)
        self._previous_parser_process = None
        self._eat_all_parser = None

    def add_to_parser(self, parser, ctx):
        def parser_process(value, state):
            value = str(value)
            while state.rargs:
                value = '{} {}'.format(value, state.rargs.pop(0))
            self._previous_parser_process(value, state)

        retval = super(OptionEatAll, self).add_to_parser(parser, ctx)

        for name in self.opts:
            our_parser = parser._long_opt.get(name) or parser._short_opt.get(
                name)
            if our_parser:
                self._eat_all_parser = our_parser
                self._previous_parser_process = our_parser.process
                our_parser.process = parser_process
                break

        return retval


@main.command(
    'predict', short_help='Use a model bundle to predict on new images.')
@click.pass_context
@click.argument('model_bundle')
@click.argument('image_uri')
@click.argument('output_uri')
@click.option(
    '--channel-order',
    cls=OptionEatAll,
    help='List of indices comprising channel_order. Example: 2 1 0')
def predict(ctx, model_bundle, image_uri, output_uri, channel_order):
    """Make predictions on the images at IMAGE_URI
    using MODEL_BUNDLE and store the prediction output at OUTPUT_URI.
    """
    if channel_order is not None:
        channel_order = [
            int(channel_ind) for channel_ind in channel_order.split(' ')
        ]

    with _rv_config.get_tmp_dir() as tmp_dir:
        predictor = Predictor(model_bundle, tmp_dir, channel_order)
        predictor.predict([image_uri], output_uri)
