# -*- coding: utf-8 -*-
from datetime import datetime
from pyqtgraph.Qt import QtCore, QtGui, QtWidgets
from pyqtgraph.pgcollections import OrderedDict
from pyqtgraph import FileDialog
from pyqtgraph.debug import printExc
from pyqtgraph import dockarea as dockarea
from ami import LogConfig
from ami.asyncqt import asyncSlot
from ami.flowchart.FlowchartGraphicsView import FlowchartGraphicsView
from ami.flowchart.Terminal import Terminal, TerminalGraphicsItem, ConnectionItem
from ami.flowchart.library import LIBRARY
from ami.flowchart.library.common import SourceNode, CtrlNode
from ami.flowchart.Node import Node, NodeGraphicsItem, find_nearest
from ami.flowchart.NodeLibrary import SourceLibrary
from ami.flowchart.SourceConfiguration import SourceConfiguration
from ami.comm import AsyncGraphCommHandler, Ports
from ami.client import flowchart_messages as fcMsgs

import ami.flowchart.Editor as EditorTemplate
import amitypes
import asyncio
import zmq.asyncio
import json
import subprocess
import re
import tempfile
import numpy as np
import networkx as nx
import itertools as it
import collections
import os
import typing  # noqa
import logging
import socket
import prometheus_client as pc


logger = logging.getLogger(LogConfig.get_package_name(__name__))


class Flowchart(Node):
    sigFileLoaded = QtCore.Signal(object)
    sigFileSaved = QtCore.Signal(object)
    sigNodeCreated = QtCore.Signal(object)
    sigNodeChanged = QtCore.Signal(object)
    # called when output is expected to have changed

    def __init__(self, name=None, filePath=None, library=None,
                 broker_addr="", graphmgr_addr="", checkpoint_addr="",
                 prometheus_dir=None, hutch=None):
        super().__init__(name)
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

        self.prometheus_dir = prometheus_dir
        self.hutch = hutch

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

    def start_prometheus(self):
        port = Ports.Prometheus
        while True:
            try:
                pc.start_http_server(port)
                break
            except OSError:
                port += 1

        if self.prometheus_dir:
            if not os.path.exists(self.prometheus_dir):
                os.makedirs(self.prometheus_dir)
            pth = f"drpami_{socket.gethostname()}_client.json"
            pth = os.path.join(self.prometheus_dir, pth)
            conf = [{"targets": [f"{socket.gethostname()}:{port}"]}]
            try:
                with open(pth, 'w') as f:
                    json.dump(conf, f)
            except PermissionError:
                logging.error("Permission denied: %s", pth)
                pass

    def setLibrary(self, lib):
        self.library = lib
        self.widget().chartWidget.buildMenu()

    def nodes(self, **kwargs):
        return self._graph.nodes(**kwargs)

    def createNode(self, nodeType=None, name=None, pos=None):
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
        node = self.library.getNodeType(nodeType)(name)

        self.addNode(node, pos)
        return node

    def addNode(self, node, pos=None):
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
        self._graph.add_node(node.name(), node=node)
        node.sigClosed.connect(self.nodeClosed)
        node.sigTerminalConnected.connect(self.nodeConnected)
        node.sigTerminalDisconnected.connect(self.nodeDisconnected)
        node.sigNodeEnabled.connect(self.nodeEnabled)
        self.sigNodeCreated.emit(node)
        if node.isChanged(True, True):
            self.sigNodeChanged.emit(node)

    @asyncSlot(object, object)
    async def nodeClosed(self, node, input_vars):
        self._graph.remove_node(node.name())
        await self.broker.send_string(node.name(), zmq.SNDMORE)
        await self.broker.send_pyobj(fcMsgs.CloseNode())
        ctrl = self.widget()
        name = node.name()

        if hasattr(node, 'to_operation'):
            self.deleted_nodes.append(name)
            self.sigNodeChanged.emit(node)
        elif isinstance(node, SourceNode):
            await ctrl.features.discard(name, name)
            await ctrl.graphCommHandler.unview(name)
        elif node.viewable():
            views = []
            for term, in_var in input_vars.items():
                discarded = await ctrl.features.discard(name, in_var)
                if discarded:
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
            self._graph.add_edge(localNode, remoteNode, key=key,
                                 from_term=localTerm.name(), to_term=remoteTerm.name())
        self.sigNodeChanged.emit(localTerm.node())

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
        self.sigNodeChanged.emit(localTerm.node())

    @asyncSlot(object)
    async def nodeEnabled(self, root):
        enabled = root._enabled

        outputs = [n for n, d in self._graph.out_degree() if d == 0]
        sources_targets = list(it.product([root.name()], outputs))
        ctrl = self.widget()
        views = []

        for s, t in sources_targets:
            paths = list(nx.algorithms.all_simple_paths(self._graph, s, t))

            for path in paths:
                for node in path:
                    node = self._graph.nodes[node]['node']
                    name = node.name()
                    node.nodeEnabled(enabled)
                    if not enabled:
                        if hasattr(node, 'to_operation'):
                            self.deleted_nodes.append(name)
                        elif node.viewable():
                            for term, in_var in node.input_vars().items():
                                discarded = await ctrl.features.discard(name, in_var)
                                if discarded:
                                    views.append(in_var)
                    else:
                        node.changed = True
                    if node.conditions():
                        preds = self._graph.predecessors(node.name())
                        preds = filter(lambda n: n.startswith("Filter"), preds)
                        for filt in preds:
                            node = self._graph.nodes[filt]['node']
                            node.nodeEnabled(enabled)

        if views:
            await ctrl.graphCommHandler.unview(views)
        await ctrl.applyClicked()

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

    def widget(self):
        """
        Return the control widget for this flowchart.

        This widget provides GUI access to the parameters for each node and a
        graphical representation of the flowchart.
        """
        if self._widget is None:
            self._widget = FlowchartCtrlWidget(self, self.graphmgr_addr)
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
        state['library'] = self.widget().libraryEditor.saveState()
        return state

    def restoreState(self, state):
        """
        Restore the state of this flowchart from a previous call to `saveState()`.
        """
        self.blockSignals(True)
        try:
            if 'source_configuration' in state:
                src_cfg = state['source_configuration']
                self.widget().sourceConfigure.restoreState(src_cfg)
                if src_cfg['files']:
                    self.widget().sourceConfigure.applyClicked()

            if 'library' in state:
                lib_cfg = state['library']
                self.widget().libraryEditor.restoreState(lib_cfg)
                self.widget().libraryEditor.applyClicked()

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
                        self.addNode(node=node)
                    except Exception:
                        printExc("Error creating node %s: (continuing anyway)" % n['name'])
                else:
                    try:
                        node = self.createNode(n['class'], name=n['name'])
                    except Exception:
                        printExc("Error creating node %s: (continuing anyway)" % n['name'])

                node.restoreState(n['state'])
                if hasattr(node, "display"):
                    node.display(topics=None, terms=None, addr=None, win=None)
                    if hasattr(node.widget, 'restoreState') and 'widget' in n['state']:
                        node.widget.restoreState(n['state']['widget'])

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

        for name, node in self.nodes(data='node'):
            self.sigNodeChanged.emit(node)

    @asyncSlot(str)
    async def loadFile(self, fileName=None):
        """
        Load a flowchart (*.fc) file.
        """
        with open(fileName, 'r') as f:
            state = json.load(f)

        ctrl = self.widget()
        await ctrl.clear()
        self.restoreState(state)
        self.viewBox.autoRange()
        self.sigFileLoaded.emit(fileName)
        await ctrl.applyClicked(build_views=False)

        nodes = []
        for name, node in self.nodes(data='node'):
            if node.viewed or node.exportable():
                nodes.append(node)

        await ctrl.chartWidget.build_views(nodes, ctrl=True, export=True)

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
        state = json.dumps(state, indent=2, separators=(',', ': '), sort_keys=True, cls=amitypes.TypeEncoder)

        with open(fileName, 'w') as f:
            f.write(state)
            f.write('\n')

        ctrl = self.widget()
        ctrl.graph_info.labels(self.hutch, ctrl.graph_name).info({'graph': state})
        ctrl.chartWidget.updateStatus(f"Saved graph to: {fileName}")
        self.sigFileSaved.emit(fileName)

    async def clear(self):
        """
        Remove all nodes from this flowchart except the original input/output nodes.
        """
        for name, gnode in self._graph.nodes().items():
            node = gnode['node']
            await self.broker.send_string(name, zmq.SNDMORE)
            await self.broker.send_pyobj(fcMsgs.CloseNode())
            node.close(emit=False)

        self._graph = nx.MultiDiGraph()

    async def updateState(self):
        while True:
            node_name = await self.checkpoint.recv_string()
            # there is something wrong with this
            # in ami.client.flowchart.NodeProcess.send_checkpoint we send a
            # fcMsgs.NodeCheckPoint but we are only ever receiving the state
            new_node_state = await self.checkpoint.recv_pyobj()
            node = self._graph.nodes[node_name]['node']
            current_node_state = node.saveState()
            restore_ctrl = False
            restore_widget = False

            if 'ctrl' in new_node_state:
                if current_node_state['ctrl'] != new_node_state['ctrl']:
                    current_node_state['ctrl'] = new_node_state['ctrl']
                    restore_ctrl = True

            if 'widget' in new_node_state:
                if current_node_state['widget'] != new_node_state['widget']:
                    restore_widget = True
                    current_node_state['widget'] = new_node_state['widget']

            if 'geometry' in new_node_state:
                node.geometry = QtCore.QByteArray.fromHex(bytes(new_node_state['geometry'], 'ascii'))

            if restore_ctrl or restore_widget:
                node.restoreState(current_node_state)
                node.changed = node.isChanged(restore_ctrl, restore_widget)
                if node.changed:
                    self.sigNodeChanged.emit(node)

            node.viewed = new_node_state['viewed']

    async def updateSources(self, init=False):
        num_workers = None

        while True:
            topic = await self.graphinfo.recv_string()
            source = await self.graphinfo.recv_string()
            msg = await self.graphinfo.recv_pyobj()

            if topic == 'sources':
                source_library = SourceLibrary()
                for source, node_type in msg.items():
                    pth = []
                    for part in source.split(':')[:-1]:
                        if pth:
                            part = ":".join((pth[-1], part))
                        pth.append(part)
                    source_library.addNodeType(source, amitypes.loads(node_type), [pth])

                self.source_library = source_library

                if init:
                    break

                ctrl = self.widget()
                tree = ctrl.ui.source_tree
                ctrl.ui.clear_model(tree)
                ctrl.ui.create_model(ctrl.ui.source_tree, self.source_library.getLabelTree())

                ctrl.chartWidget.updateStatus("Updated sources.")

            elif topic == 'event_rate':
                if num_workers is None:
                    ctrl = self.widget()
                    compiler_args = await ctrl.graphCommHandler.compilerArgs
                    num_workers = compiler_args['num_workers']
                    events_per_second = [None]*num_workers
                    total_events = [None]*num_workers

                time_per_event = msg[ctrl.graph_name]
                worker = int(re.search(r'(\d)+', source).group())
                events_per_second[worker] = len(time_per_event)/(time_per_event[-1][1] - time_per_event[0][0])
                total_events[worker] = msg['num_events']

                if all(events_per_second):
                    events_per_second = int(np.sum(events_per_second))
                    total_num_events = int(np.sum(total_events))
                    ctrl = self.widget()
                    ctrl.ui.rateLbl.setText(f"Num Events: {total_num_events} Events/Sec: {events_per_second}")
                    events_per_second = [None]*num_workers
                    total_events = [None]*num_workers

            elif topic == 'error':
                ctrl = self.widget()
                if hasattr(msg, 'node_name'):
                    node_name = ctrl.metadata[msg.node_name]['parent']
                    node = self.nodes(data='node')[node_name]
                    node.setException(msg)
                    ctrl.chartWidget.updateStatus(f"{source} {node.name()}: {msg}", color='red')
                else:
                    ctrl.chartWidget.updateStatus(f"{source}: {msg}", color='red')

    async def run(self):
        await asyncio.gather(self.updateState(),
                             self.updateSources())


class FlowchartCtrlWidget(QtGui.QWidget):
    """
    The widget that contains the list of all the nodes in a flowchart and their controls,
    as well as buttons for loading/saving flowcharts.
    """

    def __init__(self, chart, graphmgr_addr):
        super().__init__()

        self.graphCommHandler = AsyncGraphCommHandler(graphmgr_addr.name, graphmgr_addr.comm, ctx=chart.ctx)
        self.graph_name = graphmgr_addr.name
        self.metadata = None

        self.currentFileName = None
        self.chart = chart
        self.chartWidget = FlowchartWidget(chart, self)

        self.ui = EditorTemplate.Ui_Toolbar()
        self.ui.setupUi(parent=self, chart=self.chartWidget)
        self.ui.create_model(self.ui.node_tree, self.chart.library.getLabelTree())
        self.ui.create_model(self.ui.source_tree, self.chart.source_library.getLabelTree())

        self.chart.sigNodeChanged.connect(self.ui.setPending)

        self.features = Features(self.graphCommHandler)

        self.ui.actionNew.triggered.connect(self.clear)
        self.ui.actionOpen.triggered.connect(self.openClicked)
        self.ui.actionSave.triggered.connect(self.saveClicked)
        self.ui.actionSaveAs.triggered.connect(self.saveAsClicked)

        self.ui.actionConfigure.triggered.connect(self.configureClicked)
        self.ui.actionApply.triggered.connect(self.applyClicked)
        self.ui.actionReset.triggered.connect(self.resetClicked)
        # self.ui.actionProfiler.triggered.connect(self.profilerClicked)

        self.ui.actionHome.triggered.connect(self.homeClicked)
        self.ui.navGroup.triggered.connect(self.navClicked)

        self.chart.sigFileLoaded.connect(self.setCurrentFile)
        self.chart.sigFileSaved.connect(self.setCurrentFile)

        self.sourceConfigure = SourceConfiguration()
        self.sourceConfigure.sigApply.connect(self.configureApply)

        self.libraryEditor = EditorTemplate.LibraryEditor(self, chart.library)
        self.libraryEditor.sigApplyClicked.connect(self.libraryUpdated)
        self.libraryEditor.sigReloadClicked.connect(self.libraryReloaded)
        self.ui.libraryConfigure.clicked.connect(self.libraryEditor.show)

        self.graph_info = pc.Info('ami_graph', 'AMI Client graph', ['hutch', 'name'])

    @asyncSlot()
    async def applyClicked(self, build_views=True):
        graph_nodes = []
        disconnectedNodes = []
        displays = set()

        msg = QtWidgets.QMessageBox(parent=self)
        msg.setText("Failed to submit graph! See status.")

        if self.chart.deleted_nodes:
            await self.graphCommHandler.remove(self.chart.deleted_nodes)
            self.chart.deleted_nodes = []

        # detect if the manager has no graph (e.g. from a purge on failure)
        if await self.graphCommHandler.graphVersion == 0:
            # mark all the nodes as changed to force a resubmit of the whole graph
            for name, gnode in self.chart._graph.nodes().items():
                gnode = gnode['node']
                gnode.changed = True
            # reset reference counting on views
            await self.features.reset()

        changed_nodes = set()
        failed_nodes = set()
        seen = set()

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

            if gnode.changed and gnode not in changed_nodes:
                changed_nodes.add(gnode)

                if not hasattr(gnode, 'to_operation'):
                    if gnode.isSource() and gnode.viewable() and gnode.viewed:
                        displays.add(gnode)
                    elif gnode.exportable():
                        displays.add(gnode)
                    continue

                outputs = [name]
                outputs.extend(nx.algorithms.dag.descendants(self.chart._graph, name))

                for output in outputs:
                    gnode = self.chart._graph.nodes[output]
                    node = gnode['node']

                    if hasattr(node, 'to_operation') and node not in seen:
                        try:
                            nodes = node.to_operation(inputs=node.input_vars(),
                                                      conditions=node.condition_vars())
                        except Exception:
                            self.chartWidget.updateStatus(f"{node.name()} error!", color='red')
                            printExc(f"{node.name()} raised exception! See console for stacktrace.")
                            node.setException(True)
                            failed_nodes.add(node)
                            continue

                        seen.add(node)

                        if type(nodes) is list:
                            graph_nodes.extend(nodes)
                        else:
                            graph_nodes.append(nodes)

                    if (node.viewable() or node.buffered()) and node.viewed:
                        displays.add(node)

        if disconnectedNodes:
            for node in disconnectedNodes:
                self.chartWidget.updateStatus(f"{node.name()} disconnected!", color='red')
                node.setException(True)
            msg.exec()
            return

        if failed_nodes:
            self.chartWidget.updateStatus("failed to submit graph", color='red')
            msg.exec()
            return

        if graph_nodes:
            await self.graphCommHandler.add(graph_nodes)

            node_names = ', '.join(set(map(lambda node: node.parent, graph_nodes)))
            self.chartWidget.updateStatus(f"Submitted {node_names}")

        node_names = ', '.join(set(map(lambda node: node.name(), displays)))
        if displays and build_views:
            self.chartWidget.updateStatus(f"Redisplaying {node_names}")
            await self.chartWidget.build_views(displays, export=True, redisplay=True)

        for node in changed_nodes:
            node.changed = False

        self.metadata = await self.graphCommHandler.metadata
        self.ui.setPendingClear()

        version = str(await self.graphCommHandler.graphVersion)
        state = self.chart.saveState()
        state = json.dumps(state, indent=2, separators=(',', ': '), sort_keys=True, cls=amitypes.TypeEncoder)
        self.graph_info.labels(self.chart.hutch, self.graph_name).info({'graph': state, 'version': version})

    def openClicked(self):
        startDir = self.chart.filePath
        if startDir is None:
            startDir = '.'
        self.fileDialog = FileDialog(None, "Load Flowchart..", startDir, "Flowchart (*.fc)")
        self.fileDialog.show()
        self.fileDialog.fileSelected.connect(self.chart.loadFile)

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
        await self.graphCommHandler.destroy()

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
        await self.graphCommHandler.destroy()
        await self.chart.clear()
        self.chartWidget.clear()
        self.setCurrentFile(None)
        self.chart.sigFileLoaded.emit('')
        self.features = Features(self.graphCommHandler)

    def configureClicked(self):
        self.sourceConfigure.show()

    @asyncSlot(object)
    async def configureApply(self, src_cfg):
        missing = []

        if 'files' in src_cfg:
            for f in src_cfg['files']:
                if not os.path.exists(f):
                    missing.append(f)

        if not missing:
            await self.graphCommHandler.updateSources(src_cfg)
        else:
            missing = ' '.join(missing)
            self.chartWidget.updateStatus(f"Missing {missing}!", color='red')

    @asyncSlot()
    async def profilerClicked(self):
        await self.chart.broker.send_string("profiler", zmq.SNDMORE)
        await self.chart.broker.send_pyobj(fcMsgs.Profiler(name=self.graph_name, command="show"))

    @asyncSlot()
    async def libraryUpdated(self):
        await self.chart.broker.send_string("library", zmq.SNDMORE)
        await self.chart.broker.send_pyobj(fcMsgs.Library(name=self.graph_name,
                                                          paths=self.libraryEditor.paths))

        dirs = set(map(os.path.dirname, self.libraryEditor.paths))
        await self.graphCommHandler.updatePath(dirs)

        self.chartWidget.updateStatus("Loaded modules.")

    @asyncSlot(object)
    async def libraryReloaded(self, mods):
        smods = set(map(lambda mod: mod.__name__, mods))

        for name, gnode in self.chart._graph.nodes().items():
            node = gnode['node']
            if node.__module__ in smods:
                await self.chart.broker.send_string(node.name(), zmq.SNDMORE)
                await self.chart.broker.send_pyobj(fcMsgs.ReloadLibrary(name=node.name(),
                                                                        mods=smods))
                self.chartWidget.updateStatus(f"Reloaded {node.name()}.")


class FlowchartWidget(dockarea.DockArea):
    """Includes the actual graphical flowchart and debugging interface"""
    def __init__(self, chart, ctrl):
        super().__init__()
        self.chart = chart
        self.ctrl = ctrl
        self.hoverItem = None

        #  build user interface (it was easier to do it here than via developer)
        self.view = FlowchartGraphicsView(self)
        self.viewDock = dockarea.Dock('view', size=(1000, 600))
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
            self.chart.addNode(node=node, pos=pos)

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

        if not hasattr(node, 'display'):
            return

        if node.viewable():
            inputs = [n for n, d, in self.chart._graph.in_degree() if d == 0]
            seen = set()
            pending = set()

            for in_node in inputs:
                paths = list(nx.algorithms.all_simple_paths(self.chart._graph, in_node, node.name()))
                for path in paths:
                    for gnode in path:
                        gnode = self.chart._graph.nodes[gnode]
                        node = gnode['node']
                        if node in seen:
                            continue
                        else:
                            seen.add(node)

                        if node.changed:
                            pending.add(node.name())

            if pending:
                pending = ', '.join(pending)
                msg = QtWidgets.QMessageBox(parent=self)
                msg.setText(f"Pending changes for {pending}. Please apply before trying to view.")
                msg.exec()
                return

        await self.build_views([node], ctrl=True)
        self.ctrl.metadata = await self.ctrl.graphCommHandler.metadata

    async def build_views(self, nodes, ctrl=False, export=False, redisplay=False):
        views = {}
        display_args = []

        for node in nodes:
            name = node.name()

            node.display(topics=None, terms=None, addr=None, win=None)
            state = {}
            if hasattr(node.widget, 'saveState'):
                state = node.widget.saveState()

            args = {'name': name,
                    'state': state,
                    'redisplay': redisplay,
                    'geometry': node.geometry,
                    'units': node.input_units()}

            if node.buffered():
                # buffered nodes are allowed to override their topics/terms
                # this is done because they may want to view intermediate values
                args['topics'] = node.buffered_topics()
                args['terms'] = node.buffered_terms()

            elif isinstance(node, SourceNode) and node.viewable():
                new, topic = await self.ctrl.features.get(name, name)
                if new:
                    views[name] = name

                args['terms'] = {'In': name}
                args['topics'] = {name: topic}

            elif isinstance(node, Node) and node.viewable():
                topics = {}

                if len(node.inputs()) != len(node.input_vars()):
                    continue

                if node.changed:
                    await self.ctrl.features.discard(name)

                for term, in_var in node.input_vars().items():
                    new, topic = await self.ctrl.features.get(node.name(), in_var)
                    topics[in_var] = topic
                    if new:
                        views[in_var] = node.name()

                args['terms'] = node.input_vars()
                args['topics'] = topics

            elif isinstance(node, CtrlNode) and ctrl:
                args['terms'] = node.input_vars()
                args['topics'] = {}

            display_args.append(args)

            if node.exportable() and export:
                await self.ctrl.graphCommHandler.export(node.input_vars()['In'], node.values['alias'])
                if not ctrl:
                    display_args.pop()

            if not node.created:
                state = node.saveState()
                msg = fcMsgs.CreateNode(node.name(), node.__class__.__name__, state=state)
                await self.chart.broker.send_string(node.name(), zmq.SNDMORE)
                await self.chart.broker.send_pyobj(msg)
                node.created = True

        if views:
            await self.ctrl.graphCommHandler.view(views)

        for args in display_args:
            name = args['name']
            await self.chart.broker.send_string(name, zmq.SNDMORE)
            await self.chart.broker.send_pyobj(fcMsgs.DisplayNode(**args))

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

                if term.unit():
                    connections.append(f"in {term.unit()}")

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

                if term.unit():
                    connections.append(f"in {term.unit()}")

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

            if term.unit():
                text += f"\nUnit: {term.unit()}"

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
                prefix = f"from {source.node().name()}.{source.name()} to {target.node().name()}.{target.name()}\n"
                from_node = f"\nfrom: {source.node().name()}.{source.name()} type: {source.type()}"
                if source.unit():
                    from_node += f" unit: {source.unit()}"
                to_node = f"\nto: {target.node().name()}.{target.name()} type: {target.type()}"
                if target.unit():
                    to_node += f" unit: {target.unit()}"
                text = ' '.join(["Connection", prefix, from_node, to_node])

        if text:
            self.hoverText.setPlainText(text)

    def clear(self):
        self.hoverText.setPlainText('')

    def updateStatus(self, text, color='black'):
        now = datetime.now().strftime('%H:%M:%S')
        self.statusText.insertHtml(f"<font color={color}>[{now}] {text}</font>")
        self.statusText.append("")


class Features(object):

    def __init__(self, graphCommHandler):
        self.features_count = collections.defaultdict(set)
        self.features = {}
        self.graphCommHandler = graphCommHandler
        self.lock = asyncio.Lock()

    async def get(self, name, in_var):
        async with self.lock:
            if name in self.features:
                topic = self.features[in_var]
                new = False
            else:
                topic = self.graphCommHandler.auto(in_var)
                self.features[in_var] = topic
                new = True

            self.features_count[in_var].add(name)
            return new, topic

    async def discard(self, name, in_var=None):
        async with self.lock:
            if in_var and in_var in self.features_count:
                self.features_count[in_var].discard(name)
                if not self.features_count[in_var]:
                    del self.features[in_var]
                    del self.features_count[in_var]
                return True
            else:
                for in_var, viewers in self.features_count.items():
                    viewers.discard(name)
                    if not viewers and name in self.features:
                        del self.features[name]
                return True

        return False

    async def reset(self):
        async with self.lock:
            self.features = {}
            self.features_count = collections.defaultdict(set)
