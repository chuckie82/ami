from ami.flowchart.library.DisplayWidgets import ScalarWidget, ScatterWidget, WaveformWidget, \
    ImageWidget, LineWidget, ArrayWidget, HistogramWidget, Histogram2DWidget
from ami.flowchart.library.common import CtrlNode, MAX
from amitypes import Array, Array1d, Array2d
import ami.graph_nodes as gn
import asyncio


class ScalarViewer(CtrlNode):

    """
    ScalarViewer displays the value of a scalar.
    """

    nodeName = "ScalarViewer"
    uiTemplate = []

    def __init__(self, name):
        super().__init__(name,
                         terminals={"In": {"io": "in", "ttype": float}},
                         viewable=True)

    def display(self, topics, terms, addr, win, **kwargs):
        return super().display(topics, terms, addr, win, ScalarWidget, **kwargs)


class WaveformViewer(CtrlNode):

    """
    WaveformViewer displays 1D arrays.
    """

    nodeName = "WaveformViewer"
    uiTemplate = []

    def __init__(self, name):
        super().__init__(name, terminals={"In": {"io": "in", "ttype": Array1d}},
                         allowAddInput=True,
                         viewable=True)

    def display(self, topics, terms, addr, win, **kwargs):
        return super().display(topics, terms, addr, win, WaveformWidget, **kwargs)


class ImageViewer(CtrlNode):

    """
    ImageViewer displays 2D arrays.
    """

    nodeName = "ImageViewer"
    uiTemplate = []

    def __init__(self, name):
        super().__init__(name, terminals={"In": {"io": "in", "ttype": Array2d}}, viewable=True)

    def display(self, topics, terms, addr, win, **kwargs):
        return super().display(topics, terms, addr, win, ImageWidget, **kwargs)


class Histogram(CtrlNode):

    """
    Histogram plots a histogram created from Binning.
    """

    nodeName = "Histogram"
    uiTemplate = []

    def __init__(self, name):
        super().__init__(name,
                         terminals={"Bins": {"io": "in", "ttype": Array1d},
                                    "Counts": {"io": "in", "ttype": Array1d}},
                         allowAddInput=True,
                         viewable=True)

    def display(self, topics, terms, addr, win, **kwargs):
        return super().display(topics, terms, addr, win, HistogramWidget, **kwargs)

    def addInput(self, **args):
        self.addTerminal(name="Bins", io='in', ttype=Array1d, **args)
        self.addTerminal(name="Counts", io='in', ttype=Array1d, **args)


class Histogram2D(CtrlNode):

    """
    Histogram2D plots a 2d histogram created from Binning2D.
    """

    nodeName = "Histogram2D"
    uiTemplate = []

    def __init__(self, name):
        super().__init__(name,
                         terminals={"XBins": {"io": "in", "ttype": Array1d},
                                    "YBins": {"io": "in", "ttype": Array1d},
                                    "Counts": {"io": "in", "ttype": Array2d}},
                         viewable=True)

    def display(self, topics, terms, addr, win, **kwargs):
        return super().display(topics, terms, addr, win, Histogram2DWidget, **kwargs)


class ScatterPlot(CtrlNode):

    """
    Scatter Plot collects two scalars and plots them against each other.
    """

    nodeName = "ScatterPlot"
    uiTemplate = [("Num Points", 'intSpin', {'value': 100, 'min': 1, 'max': MAX})]

    def __init__(self, name):
        super().__init__(name, terminals={"X": {"io": "in", "ttype": float},
                                          "Y": {"io": "in", "ttype": float}},
                         allowAddInput=True,
                         buffered=True)

    def display(self, topics, terms, addr, win, **kwargs):
        return super().display(topics, terms, addr, win, ScatterWidget, **kwargs)

    def addInput(self, **args):
        self.addTerminal(name="X", io='in', ttype=float, **args)
        self.addTerminal(name="Y", io='in', ttype=float, **args)

    def to_operation(self, inputs, conditions={}):
        outputs = [self.name()+'.'+i for i in inputs.keys()]
        buffer_output = [self.name()]
        nodes = [gn.RollingBuffer(name=self.name()+"_buffer", N=self.Num_Points,
                                  conditions_needs=list(conditions.values()), inputs=list(inputs.values()),
                                  outputs=buffer_output, parent=self.name()),
                 gn.Map(name=self.name()+"_operation", inputs=buffer_output, outputs=outputs, func=lambda a: zip(*a),
                        parent=self.name())]
        return nodes


class ScalarPlot(CtrlNode):

    """
    Scalar Plot collects scalars and plots them.
    """

    nodeName = "ScalarPlot"
    uiTemplate = [("Num Points", 'intSpin', {'value': 100, 'min': 1, 'max': MAX})]

    def __init__(self, name):
        super().__init__(name, terminals={"Y": {"io": "in", "ttype": float}},
                         allowAddInput=True,
                         buffered=True)

    def addInput(self, **args):
        self.addTerminal(name="Y", io='in', ttype=float, **args)

    def display(self, topics, terms, addr, win, **kwargs):
        return super().display(topics, terms, addr, win, WaveformWidget, **kwargs)

    def to_operation(self, inputs, conditions={}):
        outputs = [self.name()+'.'+i for i in inputs.keys()]
        buffer_output = [self.name()]

        if len(inputs.values()) > 1:
            node = [gn.RollingBuffer(name=self.name()+"_buffer", N=self.Num_Points,
                                     conditions_needs=list(conditions.values()), inputs=list(inputs.values()),
                                     outputs=buffer_output, parent=self.name()),
                    gn.Map(name=self.name()+"_operation", inputs=buffer_output, outputs=outputs,
                           func=lambda a: zip(*a), parent=self.name())]
        else:
            node = gn.RollingBuffer(name=self.name(), N=self.Num_Points,
                                    conditions_needs=list(conditions.values()), inputs=list(inputs.values()),
                                    outputs=outputs, parent=self.name())

        return node


class LinePlot(CtrlNode):

    """
    Line Plot plots arrays.
    """

    nodeName = "LinePlot"
    uiTemplate = []

    def __init__(self, name):
        super().__init__(name, terminals={"X": {"io": "in", "ttype": Array1d},
                                          "Y": {"io": "in", "ttype": Array1d}},
                         allowAddInput=True,
                         viewable=True)

    def display(self, topics, terms, addr, win, **kwargs):
        return super().display(topics, terms, addr, win, LineWidget, **kwargs)

    def addInput(self, **args):
        group = self.nextGroupName()
        self.addTerminal(name="X", io='in', ttype=Array1d, group=group, **args)
        self.addTerminal(name="Y", io='in', ttype=Array1d, group=group, **args)


class TableView(CtrlNode):

    """
    Display array values in a table.
    """

    nodeName = "TableView"
    uiTemplate = [("Update Rate", 'combo', {'values': list(map(str, range(60, 0, -10))), 'index': 0})]

    def __init__(self, name):
        super().__init__(name, terminals={"In": {"io": "in", "ttype": Array}},
                         viewable=True)

    def display(self, topics, terms, addr, win, **kwargs):
        if self.widget is None:
            kwargs['update_rate'] = int(self.Update_Rate)
            self.widget = ArrayWidget(topics, terms, addr, win, **kwargs)

        if self.task is None:
            self.task = asyncio.ensure_future(self.widget.update())

        return self.widget

    def update(self, *args, **kwargs):
        super().update(*args, **kwargs)
        if self.widget:
            self.widget.update_rate = int(self.Update_Rate)

        if self.task:
            self.task.cancel()
            self.task = asyncio.ensure_future(self.widget.update())
