# -*- coding: utf-8 -*-
from datetime import datetime
from pyqtgraph.Qt import QtCore, QtGui
from pyqtgraph.pgcollections import OrderedDict
from pyqtgraph import FileDialog
from pyqtgraph.debug import printExc
from pyqtgraph import dockarea as dockarea
from ami.asyncqt import asyncSlot
from ami.flowchart.FlowchartGraphicsView import ViewManager
from ami.flowchart.Terminal import Terminal, TerminalGraphicsItem, ConnectionItem
from ami.flowchart.library import LIBRARY
from ami.flowchart.library.common import SourceNode, CtrlNode
from ami.flowchart.Node import Node, NodeGraphicsItem, find_nearest
from ami.flowchart.NodeLibrary import SourceLibrary
from ami.flowchart.TypeEncoder import TypeEncoder
from ami.flowchart.SourceConfiguration import SourceConfiguration
from ami.comm import AsyncGraphCommHandler
from ami.client import flowchart_messages as fcMsgs

import ami.flowchart.Editor as EditorTemplate
import amitypes
import asyncio
import zmq.asyncio
import json
import subprocess
import re
import tempfile
import networkx as nx
import itertools as it
import collections
import typing  # noqa


class Flowchart(QtCore.QObject):
    sigFileLoaded = QtCore.Signal(object)
    sigFileSaved = QtCore.Signal(object)
    sigNodeCreated = QtCore.Signal(object)
    sigChartLoaded = QtCore.Signal()
    # called when output is expected to have changed

    def __init__(self, name=None, filePath=None, library=None,
                 broker_addr="", graphmgr_addr="", checkpoint_addr=""):
        super(Flowchart, self).__init__(name)
        self.socks = []
        self.library = library or LIBRARY
        self.graphmgr_addr = graphmgr_addr
        self.source_library = None

        self.ctx = zmq.asyncio.Context()
        self.broker = self.ctx.socket(zmq.PUB)  # used to create new node processes
        self.broker.connect(broker_addr)
        self.socks.append(self.broker)

        self.graphinfo = self.ctx.socket(zmq.SUB)
        self.graphinfo.setsockopt_string(zmq.SUBSCRIBE, '')
        self.graphinfo.connect(graphmgr_addr.info)
        self.socks.append(self.graphinfo)

        self.checkpoint = self.ctx.socket(zmq.SUB)  # used to receive ctrlnode updates from processes
        self.checkpoint.setsockopt_string(zmq.SUBSCRIBE, '')
        self.checkpoint.connect(checkpoint_addr)
        self.socks.append(self.checkpoint)

        self.filePath = filePath

        self._graph = nx.MultiDiGraph()

        self.nextZVal = 10
        self._widget = None
        self._scene = None

        self.deleted_nodes = []

        self.sigChartLoaded.connect(self.chartLoaded)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def close(self):
        for sock in self.socks:
            sock.close(linger=0)
        if self._widget is not None:
            self._widget.graphCommHandler.close()
        self.ctx.term()

    def setLibrary(self, lib):
        self.library = lib
        self.widget().chartWidget.buildMenu()

    def nodes(self, **kwargs):
        return self._graph.nodes(**kwargs)

    def createNode(self, nodeType=None, name=None, node=None, pos=None):
        """Create a new Node and add it to this flowchart.
        """
        if name is None:
            n = 0
            while True:
                name = "%s.%d" % (nodeType, n)
                if name not in self._graph.nodes():
                    break
                n += 1
        # create an instance of the node
        if node is None:
            node = self.library.getNodeType(nodeType)(name)

        self.addNode(node, name, pos)

        msg = fcMsgs.CreateNode(name, nodeType)
        self.broker.send_string(name, zmq.SNDMORE)
        self.broker.send_pyobj(msg)

        self.sigNodeCreated.emit(node)
        return node

    def addNode(self, node, name, pos=None):
        """Add an existing Node to this flowchart.

        See also: createNode()
        """
        if pos is None:
            pos = [0, 0]
        if type(pos) in [QtCore.QPoint, QtCore.QPointF]:
            pos = [pos.x(), pos.y()]
        item = node.graphicsItem()
        item.setZValue(self.nextZVal*2)
        self.nextZVal += 1
        self.viewBox.addItem(item)
        pos = (find_nearest(pos[0]), find_nearest(pos[1]))
        item.moveBy(*pos)
        self._graph.add_node(name, node=node)
        node.sigClosed.connect(self.nodeClosed)
        node.sigTerminalConnected.connect(self.nodeConnected)
        node.sigTerminalDisconnected.connect(self.nodeDisconnected)
        node.sigNodeEnabled.connect(self.nodeEnabled)

    @asyncSlot(object, object)
    async def nodeClosed(self, node, input_vars):
        self._graph.remove_node(node.name())
        await self.broker.send_string(node.name(), zmq.SNDMORE)
        await self.broker.send_pyobj(fcMsgs.CloseNode())
        ctrl = self.widget()

        if hasattr(node, 'to_operation'):
            self.deleted_nodes.append(node.name())
        elif isinstance(node, SourceNode):
            async with ctrl.features_lock:
                ctrl.features_count[node.name()].discard(node.name())
                if not ctrl.features_count[node.name()]:
                    await ctrl.graphCommHandler.unview(node.name())
        elif node.viewable():
            views = []
            async with ctrl.features_lock:
                for term, in_var in input_vars.items():
                    ctrl.features_count[in_var].discard(node.name())
                    if not ctrl.features_count[in_var]:
                        views.append(in_var)

            if views:
                await ctrl.graphCommHandler.unview(views)

    def nodeConnected(self, localTerm, remoteTerm):
        if remoteTerm.isOutput() or localTerm.isCondition():
            t = remoteTerm
            remoteTerm = localTerm
            localTerm = t

        localNode = localTerm.node().name()
        remoteNode = remoteTerm.node().name()
        key = localNode + '.' + localTerm.name() + '->' + remoteNode + '.' + remoteTerm.name()
        if not self._graph.has_edge(localNode, remoteNode, key=key):
            self._graph.add_edge(localNode, remoteNode, key=key, from_term=localTerm.name(), to_term=remoteTerm.name())

    def nodeDisconnected(self, localTerm, remoteTerm):
        if remoteTerm.isOutput() or localTerm.isCondition():
            t = remoteTerm
            remoteTerm = localTerm
            localTerm = t

        localNode = localTerm.node().name()
        remoteNode = remoteTerm.node().name()
        key = localNode + '.' + localTerm.name() + '->' + remoteNode + '.' + remoteTerm.name()
        if self._graph.has_edge(localNode, remoteNode, key=key):
            self._graph.remove_edge(localNode, remoteNode, key=key)

    def nodeEnabled(self, root):
        enabled = root._enabled

        outputs = [n for n, d in self._graph.out_degree() if d == 0]
        sources_targets = list(it.product([root.name()], outputs))

        for s, t in sources_targets:
            paths = list(nx.algorithms.all_simple_paths(self._graph, s, t))

            for path in paths:
                for node in path[1:]:
                    node = self._graph.nodes[node]['node']
                    node.nodeEnabled(enabled)
                    if node.conditions():
                        preds = self._graph.predecessors(node.name())
                        preds = filter(lambda n: n.startswith("Filter"), preds)
                        for filt in preds:
                            node = self._graph.nodes[filt]['node']
                            node.nodeEnabled(enabled)

    def connectTerminals(self, term1, term2, type_file=None):
        """Connect two terminals together within this flowchart."""
        term1.connectTo(term2, type_file=type_file)

    def chartGraphicsItem(self):
        """
        Return the graphicsItem that displays the internal nodes and
        connections of this flowchart.

        Note that the similar method `graphicsItem()` is inherited from Node
        and returns the *external* graphical representation of this flowchart."""
        return self.viewBox

    def widget(self, parent=None):
        """
        Return the control widget for this flowchart.

        This widget provides GUI access to the parameters for each node and a
        graphical representation of the flowchart.
        """
        if self._widget is None:
            self._widget = FlowchartCtrlWidget(self, self.graphmgr_addr, parent)
            self.scene = self._widget.scene()
            self.viewBox = self._widget.viewBox()
        return self._widget

    def saveState(self):
        """
        Return a serializable data structure representing the current state of this flowchart.
        """
        state = {}
        state['nodes'] = []
        state['connects'] = []
        state['viewbox'] = self.viewBox.saveState()

        for name, node in self.nodes(data='node'):
            cls = type(node)
            clsName = cls.__name__
            ns = {'class': clsName, 'name': name, 'state': node.saveState()}
            state['nodes'].append(ns)

        for from_node, to_node, data in self._graph.edges(data=True):
            from_term = data['from_term']
            to_term = data['to_term']
            state['connects'].append((from_node, from_term, to_node, to_term))

        state['source_configuration'] = self.widget().sourceConfigure.saveState()
        return state

    def restoreState(self, state, clear=False):
        """
        Restore the state of this flowchart from a previous call to `saveState()`.
        """
        self.blockSignals(True)
        try:
            if clear:
                self.clear()

            if 'source_configuration' in state:
                src_cfg = state['source_configuration']
                self.widget().sourceConfigure.restoreState(src_cfg)
                if src_cfg['files']:
                    self.widget().sourceConfigure.applyClicked()

            if 'viewbox' in state:
                self.viewBox.restoreState(state['viewbox'])

            nodes = state['nodes']
            nodes.sort(key=lambda a: a['state']['pos'][0])
            for n in nodes:
                if n['class'] == 'SourceNode':
                    try:
                        ttype = eval(n['state']['terminals']['Out']['ttype'])
                        n['state']['terminals']['Out']['ttype'] = ttype
                        node = SourceNode(name=n['name'], terminals=n['state']['terminals'])
                        node.restoreState(n['state'])
                        self.createNode(name=n['name'], node=node, nodeType=ttype)
                    except Exception:
                        printExc("Error creating node %s: (continuing anyway)" % n['name'])
                else:
                    try:
                        node = self.createNode(n['class'], name=n['name'])
                        node.restoreState(n['state'])
                        if hasattr(node, "display"):
                            node.display(topics=None, terms=None, addr=None, win=None)
                            if hasattr(node.widget, 'restoreState') and 'widget' in n['state']:
                                node.widget.restoreState(n['state']['widget'])

                    except Exception:
                        printExc("Error creating node %s: (continuing anyway)" % n['name'])

            # self.restoreTerminals(state['terminals'])

            connections = {}
            with tempfile.NamedTemporaryFile(mode='w') as type_file:
                type_file.write("from mypy_extensions import TypedDict\n")
                type_file.write("from typing import *\n")
                type_file.write("import numbers\n")
                type_file.write("import amitypes\n")
                type_file.write("T = TypeVar('T')\n\n")

                nodes = self.nodes(data='node')

                for n1, t1, n2, t2 in state['connects']:
                    try:
                        node1 = nodes[n1]
                        term1 = node1[t1]
                        node2 = nodes[n2]
                        term2 = node2[t2]

                        self.connectTerminals(term1, term2, type_file)
                        if term1.isInput() or term1.isCondition:
                            in_name = node1.name() + '_' + term1.name()
                            in_name = in_name.replace('.', '_')
                            out_name = node2.name() + '_' + term2.name()
                            out_name = out_name.replace('.', '_')
                        else:
                            in_name = node2.name() + '_' + term2.name()
                            in_name = in_name.replace('.', '_')
                            out_name = node1.name() + '_' + term1.name()
                            out_name = out_name.replace('.', '_')

                        connections[(in_name, out_name)] = (term1, term2)
                    except Exception:
                        print(node1.terminals)
                        print(node2.terminals)
                        printExc("Error connecting terminals %s.%s - %s.%s:" % (n1, t1, n2, t2))

                type_file.flush()
                status = subprocess.run(["mypy", "--follow-imports", "silent", type_file.name],
                                        capture_output=True, text=True)
                if status.returncode != 0:
                    lines = status.stdout.split('\n')[:-1]
                    for line in lines:
                        m = re.search(r"\"+(\w+)\"+", line)
                        if m:
                            m = m.group().replace('"', '')
                            for i in connections:
                                if i[0] == m:
                                    term1, term2 = connections[i]
                                    term1.disconnectFrom(term2)
                                    break
                                elif i[1] == m:
                                    term1, term2 = connections[i]
                                    term1.disconnectFrom(term2)
                                    break

        finally:
            self.blockSignals(False)

        self.sigChartLoaded.emit()

    def loadFile(self, fileName=None, startDir=None):
        """
        Load a flowchart (*.fc) file.
        """
        if fileName is None:
            if startDir is None:
                startDir = self.filePath
            if startDir is None:
                startDir = '.'
            self.fileDialog = FileDialog(None, "Load Flowchart..", startDir, "Flowchart (*.fc)")
            self.fileDialog.show()
            self.fileDialog.fileSelected.connect(self.loadFile)
            return
            #  NOTE: was previously using a real widget for the file dialog's parent,
            #        but this caused weird mouse event bugs..

        with open(fileName, 'r') as f:
            state = json.load(f)

        self.restoreState(state, clear=True)
        self.viewBox.autoRange()
        self.sigFileLoaded.emit(fileName)

    def saveFile(self, fileName=None, startDir=None, suggestedFileName='flowchart.fc'):
        """
        Save this flowchart to a .fc file
        """
        if fileName is None:
            if startDir is None:
                startDir = self.filePath
            if startDir is None:
                startDir = '.'
            self.fileDialog = FileDialog(None, "Save Flowchart..", startDir, "Flowchart (*.fc)")
            self.fileDialog.setAcceptMode(QtGui.QFileDialog.AcceptSave)
            self.fileDialog.show()
            self.fileDialog.fileSelected.connect(self.saveFile)
            return

        if not fileName.endswith('.fc'):
            fileName += ".fc"

        state = self.saveState()

        with open(fileName, 'w') as f:
            json.dump(state, f, indent=2, separators=(',', ': '), sort_keys=True, cls=TypeEncoder)
            f.write('\n')

        self.sigFileSaved.emit(fileName)

    def clear(self):
        """
        Remove all nodes from this flowchart except the original input/output nodes.
        """
        for name, node in list(self.nodes(data='node')):
            node.close()  # calls self.nodeClosed(n) by signal

    async def updateState(self):
        while True:
            node_name = await self.checkpoint.recv_string()
            # there is something wrong with this
            # in ami.client.flowchart.NodeProcess.send_checkpoint we send a
            # fcMsgs.NodeCheckPoint but we are only ever receiving the state
            new_node_state = await self.checkpoint.recv_pyobj()
            current_node_state = self._graph.nodes[node_name]['node'].saveState()

            if 'ctrl' in new_node_state:
                if current_node_state['ctrl'] != new_node_state['ctrl']:
                    current_node_state['ctrl'] = new_node_state['ctrl']
                    self._graph.nodes[node_name]['node'].changed = True

            if 'widget' in new_node_state:
                current_node_state['widget'] = new_node_state['widget']

            self._graph.nodes[node_name]['node'].restoreState(current_node_state)
            self._graph.nodes[node_name]['node'].viewed = new_node_state['viewed']

    async def updateSources(self, init=False):
        while True:
            topic = await self.graphinfo.recv_string()
            source = await self.graphinfo.recv_string()
            msg = await self.graphinfo.recv_pyobj()
            now = datetime.now()

            if topic == 'sources':
                source_library = SourceLibrary()
                for source, node_type in msg.items():
                    pth = source.split(':')
                    if len(pth) > 2:
                        pth = pth[:-1]
                    source_library.addNodeType(source, amitypes.loads(node_type), [pth])

                self.source_library = source_library

                if init:
                    break

                ctrl = self.widget()
                tree = ctrl.ui.source_tree
                ctrl.ui.clear_model(tree)
                ctrl.ui.create_model(ctrl.ui.source_tree, self.source_library.getLabelTree())

                ctrl.chartWidget.statusText.append(f"[{now.strftime('%H:%M:%S')}] Updated sources.")

            elif topic == 'error':
                ctrl = self.widget()
                if hasattr(msg, 'node_name'):
                    node_name = ctrl.metadata[msg.node_name]['parent']
                    node = self.nodes(data='node')[node_name]
                    node.setException(msg)
                    ctrl.chartWidget.statusText.append(f"[{now.strftime('%H:%M:%S')}] {source} {node.name()}: {msg}")
                else:
                    ctrl.chartWidget.statusText.append(f"[{now.strftime('%H:%M:%S')}] {source}: {msg}")
            elif topic == "times":
                pass
                # print(source, msg)

    @asyncSlot()
    async def chartLoaded(self):
        for name, node in self.nodes(data='node'):
            state = node.saveState()
            msg = fcMsgs.NodeCheckpoint(node.name(), state=state)
            await self.broker.send_string(node.name(), zmq.SNDMORE)
            await self.broker.send_pyobj(msg)

    async def run(self):
        await asyncio.gather(self.updateState(),
                             self.updateSources())


class FlowchartCtrlWidget(QtGui.QWidget):
    """
    The widget that contains the list of all the nodes in a flowchart and their controls,
    as well as buttons for loading/saving flowcharts.
    """

    def __init__(self, chart, graphmgr_addr, parent=None):
        super(FlowchartCtrlWidget, self).__init__(parent)

        self.graphCommHandler = AsyncGraphCommHandler(graphmgr_addr.name, graphmgr_addr.comm, ctx=chart.ctx)
        self.metadata = None

        self.currentFileName = None
        self.chart = chart
        self.chartWidget = FlowchartWidget(chart, self)

        self.ui = EditorTemplate.Ui_Toolbar()
        self.ui.setupUi(parent=self, chart=self.chartWidget)
        self.ui.create_model(self.ui.node_tree, self.chart.library.getLabelTree())
        self.ui.create_model(self.ui.source_tree, self.chart.source_library.getLabelTree())

        self.features_lock = asyncio.Lock()
        self.features = {}  # {in_var : topic}
        self.features_count = collections.defaultdict(set)  # {in_var : set(nodes)}

        self.ui.actionNew.triggered.connect(self.clear)
        self.ui.actionOpen.triggered.connect(self.openClicked)
        self.ui.actionSave.triggered.connect(self.saveClicked)
        self.ui.actionSaveAs.triggered.connect(self.saveAsClicked)
        self.ui.actionConfigure.triggered.connect(self.configureClicked)

        self.ui.actionApply.triggered.connect(self.applyClicked)
        self.ui.actionReset.triggered.connect(self.resetClicked)

        self.ui.actionHome.triggered.connect(self.homeClicked)
        self.ui.navGroup.triggered.connect(self.navClicked)

        self.chart.sigFileLoaded.connect(self.setCurrentFile)
        self.chart.sigFileSaved.connect(self.fileSaved)

        self.sourceConfigure = SourceConfiguration()
        self.sourceConfigure.sigApply.connect(self.configureApply)

    @asyncSlot()
    async def applyClicked(self):
        graph_nodes = set()
        disconnectedNodes = []
        displays = set()
        now = datetime.now().strftime('%H:%M:%S')

        if self.chart.deleted_nodes:
            await self.graphCommHandler.remove(self.chart.deleted_nodes)
            self.chart.deleted_nodes = []

        outputs = [n for n, d in self.chart._graph.out_degree() if d == 0]

        for name, gnode in self.chart._graph.nodes().items():
            gnode = gnode['node']
            if not gnode.enabled():
                continue

            if not gnode.hasInput():
                disconnectedNodes.append(gnode)
                continue
            elif gnode.exception:
                gnode.clearException()
                gnode.recolor()

            if not hasattr(gnode, 'to_operation'):
                continue

            if gnode.changed:
                gnode.changed = False

                for output in outputs:
                    paths = list(nx.algorithms.all_simple_paths(self.chart._graph, name, output))

                    for path in paths:
                        for gnode in path:
                            gnode = self.chart._graph.nodes[gnode]
                            node = gnode['node']

                            if hasattr(node, 'to_operation'):
                                nodes = node.to_operation(inputs=node.input_vars(), conditions=node.condition_vars())
                                if type(nodes) is list:
                                    graph_nodes.update(nodes)
                                else:
                                    graph_nodes.add(nodes)

                            if (node.viewable() or node.buffered()) and node.viewed:
                                displays.add(node)

        if disconnectedNodes:
            for node in disconnectedNodes:
                self.chartWidget.statusText.append(f"[{now}] {node.name()} disconnected!")
                node.setException(True)
            return

        if not graph_nodes:
            return

        await self.graphCommHandler.add(list(graph_nodes))

        node_names = ', '.join(set(map(lambda node: node.parent, graph_nodes)))
        self.chartWidget.statusText.append(f"[{now}] Submitted {node_names}")

        node_names = ', '.join(set(map(lambda node: node.name(), displays)))
        if node_names:
            self.chartWidget.statusText.append(f"[{now}] Redisplaying {node_names}")

        for node in displays:

            if node.buffered():
                topics = node.buffered_topics()
                terms = node.buffered_terms()

                node.display(topics=None, terms=None, addr=None, win=None)
                state = {}
                if hasattr(node.widget, 'saveState'):
                    state = node.widget.saveState()

                await self.chart.broker.send_string(node.name(), zmq.SNDMORE)
                await self.chart.broker.send_pyobj(fcMsgs.DisplayNode(name=node.name(),
                                                                      topics=topics,
                                                                      state=state,
                                                                      terms=terms,
                                                                      redisplay=True))

            elif node.viewable():
                topics = []
                views = {}
                for term, in_var in node.input_vars().items():
                    if in_var in self.features:
                        topic = self.features[in_var]
                    else:
                        topic = self.graphCommHandler.auto(in_var)
                        async with self.features_lock:
                            self.features[in_var] = topic
                            self.features_count[in_var].add(node.name())
                        views[in_var] = node.name()

                    topics.append((in_var, topic))

                if views:
                    await self.graphCommHandler.view(views)

                node.display(topics=None, terms=None, addr=None, win=None)
                state = {}
                if hasattr(node.widget, 'saveState'):
                    state = node.widget.saveState()

                await self.chart.broker.send_string(node.name(), zmq.SNDMORE)
                await self.chart.broker.send_pyobj(fcMsgs.DisplayNode(name=node.name(),
                                                                      topics=dict(topics),
                                                                      state=state,
                                                                      terms=node.input_vars(),
                                                                      redisplay=True))

            elif node.exportable():
                await self.graphCommHandler.export(node.input_vars()['In'], node.name())

        self.metadata = await self.graphCommHandler.metadata

    def openClicked(self):
        self.chart.loadFile()

    def fileSaved(self, fileName):
        self.setCurrentFile(fileName)

    def saveClicked(self):
        if self.currentFileName is None:
            self.saveAsClicked()
        else:
            try:
                self.chart.saveFile(self.currentFileName)
            except Exception as e:
                raise e

    def saveAsClicked(self):
        try:
            if self.currentFileName is None:
                self.chart.saveFile()
            else:
                self.chart.saveFile(suggestedFileName=self.currentFileName)
        except Exception as e:
            raise e

    def setCurrentFile(self, fileName):
        self.currentFileName = fileName

    def homeClicked(self):
        children = self.viewBox().allChildren()
        self.viewBox().autoRange(items=children)

    def navClicked(self, action):
        if action == self.ui.actionPan:
            self.viewBox().setMouseMode("Pan")
        elif action == self.ui.actionSelect:
            self.viewBox().setMouseMode("Select")
        elif action == self.ui.actionComment:
            self.viewBox().setMouseMode("Comment")

    @asyncSlot()
    async def resetClicked(self):
        for name, gnode in self.chart._graph.nodes().items():
            gnode = gnode['node']
            gnode.changed = True

        await self.applyClicked()

    def scene(self):
        # returns the GraphicsScene object
        return self.chartWidget.scene()

    def viewBox(self):
        return self.chartWidget.viewBox()

    def chartWidget(self):
        return self.chartWidget

    @asyncSlot()
    async def clear(self):
        self.chart.clear()
        self.chartWidget.clear()
        self.setCurrentFile(None)
        self.chart.sigFileLoaded.emit('')
        await self.applyClicked()

    def configureClicked(self):
        self.sourceConfigure.show()

    @asyncSlot(object)
    async def configureApply(self, src_cfg):
        await self.graphCommHandler.updateSources(src_cfg)


class FlowchartWidget(dockarea.DockArea):
    """Includes the actual graphical flowchart and debugging interface"""
    def __init__(self, chart, ctrl):
        super().__init__()
        self.chart = chart
        self.ctrl = ctrl
        self.hoverItem = None

        #  build user interface (it was easier to do it here than via developer)
        self.view = ViewManager(self, ctrl)
        self.viewDock = dockarea.Dock('view', size=(1000, 600))
        self.viewDock.nStyle = ""
        self.viewDock.addWidget(self.view)
        self.viewDock.hideTitleBar()
        self.addDock(self.viewDock)

        self.hoverText = QtGui.QTextEdit()
        self.hoverText.setReadOnly(True)
        self.hoverDock = dockarea.Dock('Hover Info', size=(1000, 20))
        self.hoverDock.addWidget(self.hoverText)
        self.addDock(self.hoverDock, 'bottom')

        self.statusText = QtGui.QTextEdit()
        self.statusText.setReadOnly(True)
        self.statusDock = dockarea.Dock('Status', size=(1000, 20))
        self.statusDock.addWidget(self.statusText)
        self.addDock(self.statusDock, 'bottom')

        self._scene = self.view.scene()
        self._viewBox = self.view.viewBox()

        self._scene.selectionChanged.connect(self.selectionChanged)
        self._scene.sigMouseHover.connect(self.hoverOver)

    def reloadLibrary(self):
        self.operationMenu.triggered.disconnect(self.operationMenuTriggered)
        self.operationMenu = None
        self.subMenus = []
        self.chart.library.reload()
        self.buildMenu()

    def buildOperationMenu(self, pos=None):
        def buildSubMenu(node, rootMenu, subMenus, pos=None):
            for section, node in node.items():
                menu = QtGui.QMenu(section)
                rootMenu.addMenu(menu)
                if isinstance(node, OrderedDict):
                    buildSubMenu(node, menu, subMenus, pos=pos)
                    subMenus.append(menu)
                else:
                    act = rootMenu.addAction(section)
                    act.nodeType = section
                    act.pos = pos
        self.operationMenu = QtGui.QMenu()
        self.operationSubMenus = []
        buildSubMenu(self.chart.library.getNodeTree(), self.operationMenu, self.operationSubMenus, pos=pos)
        self.operationMenu.triggered.connect(self.operationMenuTriggered)
        return self.operationMenu

    def buildSourceMenu(self, pos=None):
        def buildSubMenu(node, rootMenu, subMenus, pos=None):
            for section, node in node.items():
                menu = QtGui.QMenu(section)
                rootMenu.addMenu(menu)
                if isinstance(node, OrderedDict):
                    buildSubMenu(node, menu, subMenus, pos=pos)
                    subMenus.append(menu)
                else:
                    act = rootMenu.addAction(section)
                    act.nodeType = section
                    act.pos = pos
        self.sourceMenu = QtGui.QMenu()
        self.sourceSubMenus = []
        buildSubMenu(self.chart.source_library.getSourceTree(), self.sourceMenu, self.sourceSubMenus, pos=pos)
        self.sourceMenu.triggered.connect(self.sourceMenuTriggered)
        return self.sourceMenu

    def scene(self):
        return self._scene  # the GraphicsScene item

    def viewBox(self):
        return self._viewBox  # the viewBox that items should be added to

    def operationMenuTriggered(self, action):
        nodeType = action.nodeType
        pos = self.viewBox().mapSceneToView(action.pos)
        self.chart.createNode(nodeType, pos=pos)

    def sourceMenuTriggered(self, action):
        node = action.nodeType
        if node not in self.chart._graph:
            pos = self.viewBox().mapSceneToView(action.pos)
            node_type = self.chart.source_library.getSourceType(node)
            node = SourceNode(name=node, terminals={'Out': {'io': 'out', 'ttype': node_type}})
            self.chart.createNode(node_type, name=node.name(), node=node, pos=pos)

    @asyncSlot()
    async def selectionChanged(self):
        # print "FlowchartWidget.selectionChanged called."
        items = self._scene.selectedItems()

        if len(items) != 1:
            return

        item = items[0]
        if not hasattr(item, 'node'):
            return

        node = item.node
        if not node.enabled():
            return

        node.viewed = True
        if isinstance(item.node, Node) and item.node.buffered():
            # buffered nodes are allowed to override their topics/terms
            # this is done because they may want to view intermediate values
            topics = node.buffered_topics()
            terms = node.buffered_terms()

            node.display(topics=None, terms=None, addr=None, win=None)
            state = {}
            if hasattr(node.widget, 'saveState'):
                state = node.widget.saveState()

            await self.chart.broker.send_string(node.name(), zmq.SNDMORE)
            await self.chart.broker.send_pyobj(fcMsgs.DisplayNode(name=node.name(),
                                                                  topics=topics,
                                                                  state=state,
                                                                  terms=terms))

        elif isinstance(item.node, SourceNode) and item.node.viewable():
            name = node.name()
            topic = None
            views = {}
            terms = {"In": name}

            if name in self.ctrl.features:
                topic = self.ctrl.features[name]
                async with self.ctrl.features_lock:
                    self.ctrl.features_count[name].add(name)
            else:
                topic = self.ctrl.graphCommHandler.auto(name)
                async with self.ctrl.features_lock:
                    self.ctrl.features[name] = topic
                    self.ctrl.features_count[name].add(name)
                views = {name: name}

            topics = [(name, topic)]

            if views:
                await self.ctrl.graphCommHandler.view(views)

            await self.chart.broker.send_string(node.name(), zmq.SNDMORE)
            await self.chart.broker.send_pyobj(fcMsgs.DisplayNode(name=node.name(),
                                                                  topics=dict(topics),
                                                                  terms=terms))

        elif isinstance(item.node, Node) and item.node.viewable():
            topics = []
            views = {}

            if len(node.inputs()) != len(node.input_vars()):
                return

            for term, in_var in node.input_vars().items():

                if in_var in self.ctrl.features:
                    topic = self.ctrl.features[in_var]
                    async with self.ctrl.features_lock:
                        self.ctrl.features_count[in_var].add(node.name())
                else:
                    topic = self.ctrl.graphCommHandler.auto(in_var)
                    async with self.ctrl.features_lock:
                        self.ctrl.features[in_var] = topic
                        self.ctrl.features_count[in_var].add(node.name())
                    views[in_var] = node.name()

                topics.append((in_var, topic))

            if views:
                await self.ctrl.graphCommHandler.view(views)

            node.display(topics=None, terms=None, addr=None, win=None)
            state = {}
            if hasattr(node.widget, 'saveState'):
                state = node.widget.saveState()

            await self.chart.broker.send_string(node.name(), zmq.SNDMORE)
            await self.chart.broker.send_pyobj(fcMsgs.DisplayNode(name=node.name(),
                                                                  topics=dict(topics),
                                                                  state=state,
                                                                  terms=node.input_vars()))

        elif isinstance(item.node, CtrlNode):
            await self.chart.broker.send_string(node.name(), zmq.SNDMORE)
            await self.chart.broker.send_pyobj(fcMsgs.DisplayNode(name=node.name(), topics={}, terms=node.input_vars()))

        self.ctrl.metadata = await self.ctrl.graphCommHandler.metadata

    def hoverOver(self, items):
        obj = None

        for item in items:
            if isinstance(item, NodeGraphicsItem):
                obj = item.node
            if isinstance(item, TerminalGraphicsItem):
                obj = item.term
                break
            elif isinstance(item, ConnectionItem):
                obj = item
                break

        text = ""

        if isinstance(obj, Node) and not obj.isSource():
            node = obj
            doc = node.__doc__
            doc = doc.lstrip().rstrip()
            doc = re.sub(r'(\t+)|(  )+', '', doc)
            text = [doc]

            if node.inputs():
                text.append("\nInputs:")

            for name, term in node.inputs().items():
                term = term()
                connections = []
                connections.append(f"{node.name()}.{name}")
                if term.inputTerminals():
                    connections.append("connected to:")
                else:
                    connections.append(f"accepts type: {term.type()}")
                for in_term in term.inputTerminals():
                    connections.append(f"{in_term.node().name()}.{in_term.name()}")
                text.append(' '.join(connections))

            if node.outputs():
                text.append("\nOutputs:")

            for name, term in node.outputs().items():
                term = term()
                connections = []
                connections.append(f"{node.name()}.{name}")
                if term.dependentTerms():
                    connections.append("connected to:")
                else:
                    connections.append(f"emits type: {term.type()}")
                for in_term in term.dependentTerms():
                    connections.append(f"{in_term.node().name()}.{in_term.name()}")
                text.append(' '.join(connections))

            text = '\n'.join(text)

        elif isinstance(obj, Terminal):
            term = obj
            node = obj.node()
            text = f"Term: {node.name()}.{term.name()}\nType: {term.type()}"
            terms = None

            if (term.isOutput or term.isCondition) and term.dependentTerms():
                terms = term.dependentTerms()
            elif term.isInput and term.inputTerminals():
                terms = term.inputTerminals()

            if terms:
                connections = ["Connected to:"]
                for in_term in terms:
                    connections.append(f"{in_term.node().name()}.{in_term.name()}")
                connections = ' '.join(connections)
                text = '\n'.join([text, connections])
            # self.hoverLabel.setCursorPosition(0)
        elif isinstance(obj, ConnectionItem):
            connection = obj
            source = None
            target = None

            if isinstance(connection.source, TerminalGraphicsItem):
                source = connection.source.term
            if isinstance(connection.target, TerminalGraphicsItem):
                target = connection.target.term

            if source and target:
                source = f"from {source.node().name()}.{source.name()}"
                target = f"to {target.node().name()}.{target.name()}"
                text = ' '.join(["Connection", source, target])

        if text:
            self.hoverText.setPlainText(text)

    def clear(self):
        self.hoverText.setPlainText('')
