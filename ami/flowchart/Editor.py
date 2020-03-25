from qtpy import QtGui, QtWidgets, QtCore
from pyqtgraph import dockarea


class SearchProxyModel(QtCore.QSortFilterProxyModel):

    def setFilterRegExp(self, pattern):
        if isinstance(pattern, str):
            pattern = QtCore.QRegExp(
                pattern, QtCore.Qt.CaseInsensitive,
                QtCore.QRegExp.FixedString)
        super(SearchProxyModel, self).setFilterRegExp(pattern)

    def _accept_index(self, idx):
        if idx.isValid():
            text = idx.data(QtCore.Qt.DisplayRole)
            if self.filterRegExp().indexIn(text) >= 0:
                return True
            for row in range(idx.model().rowCount(idx)):
                if self._accept_index(idx.model().index(row, 0, idx)):
                    return True
        return False

    def filterAcceptsRow(self, sourceRow, sourceParent):
        idx = self.sourceModel().index(sourceRow, 0, sourceParent)
        return self._accept_index(idx)


def build_model():
    model = SearchProxyModel()
    model.setSourceModel(QtGui.QStandardItemModel())
    model.setDynamicSortFilter(True)
    model.setFilterCaseSensitivity(QtCore.Qt.CaseInsensitive)
    return model


def build_tree(model):
    tree = QtGui.QTreeView()
    tree.setSortingEnabled(True)
    tree.sortByColumn(0, QtCore.Qt.AscendingOrder)
    tree.setEditTriggers(QtGui.QAbstractItemView.NoEditTriggers)
    tree.setHeaderHidden(True)
    tree.setRootIsDecorated(True)
    tree.setUniformRowHeights(True)
    tree.setModel(model)
    tree.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
    tree.setDragEnabled(True)
    return tree


class Ui_Toolbar(object):
    def setupUi(self, parent=None, chart=None):
        self.gridLayout = QtWidgets.QGridLayout(parent)

        self.toolBar = QtWidgets.QToolBar(parent)
        self.toolBar.setObjectName("toolBar")

        # new
        self.actionNew = QtWidgets.QAction(parent)
        icon = QtGui.QIcon.fromTheme("document-new")
        self.actionNew.setIcon(icon)
        self.actionNew.setIconText("New")
        self.actionNew.setObjectName("actionNew")

        # open
        self.actionOpen = QtWidgets.QAction(parent)
        icon = QtGui.QIcon.fromTheme("document-open")
        self.actionOpen.setIcon(icon)
        self.actionOpen.setIconText("Open")
        self.actionOpen.setObjectName("actionOpen")

        # save
        self.actionSave = QtWidgets.QAction(parent)
        icon = QtGui.QIcon.fromTheme("document-save")
        self.actionSave.setIcon(icon)
        self.actionSave.setIconText("Save")
        self.actionSave.setObjectName("actionSave")

        # save as
        self.actionSaveAs = QtWidgets.QAction(parent)
        icon = QtGui.QIcon.fromTheme("document-save")
        self.actionSaveAs.setIcon(icon)
        self.actionSaveAs.setIconText("Save As")
        self.actionSaveAs.setObjectName("actionSaveAs")

        # configure
        self.actionConfigure = QtWidgets.QAction(parent)
        icon = QtGui.QIcon.fromTheme("preferences-system")
        self.actionConfigure.setIcon(icon)
        self.actionConfigure.setIconText("Configure")
        self.actionConfigure.setObjectName("actionConfigure")

        # apply
        self.actionApply = QtWidgets.QAction(parent)
        icon = QtGui.QIcon.fromTheme("media-playback-start")
        self.actionApply.setIcon(icon)
        self.actionApply.setIconText("Apply")
        self.actionApply.setObjectName("actionApply")

        # reset
        self.actionReset = QtWidgets.QAction(parent)
        # icon = QtGui.QIcon.fromTheme("media-playback-start")
        # self.actionApply.setIcon(icon)
        self.actionReset.setIconText("Reset")
        self.actionReset.setObjectName("actionReset")

        # home
        self.actionHome = QtWidgets.QAction(parent)
        icon = QtGui.QIcon.fromTheme("go-home")
        self.actionHome.setIcon(icon)
        self.actionHome.setIconText("Home")
        self.actionHome.setObjectName("actionHome")

        self.navGroup = QtWidgets.QActionGroup(parent)

        # pan
        self.actionPan = QtWidgets.QAction(parent)
        # icon = QtGui.QIcon.fromTheme("")
        # self.actionSelect.setIcon(icon)
        self.actionPan.setIconText("Pan")
        self.actionPan.setObjectName("actionPan")
        self.actionPan.setCheckable(True)
        self.actionPan.setChecked(True)
        self.navGroup.addAction(self.actionPan)

        # select
        self.actionSelect = QtWidgets.QAction(parent)
        icon = QtGui.QIcon.fromTheme("draw-selection")
        self.actionSelect.setIcon(icon)
        self.actionSelect.setIconText("Select")
        self.actionSelect.setObjectName("actionSelect")
        self.actionSelect.setCheckable(True)
        self.navGroup.addAction(self.actionSelect)

        # comment
        self.actionComment = QtWidgets.QAction(parent)
        # icon = QtGui.QIcon.fromTheme("")
        # self.actionSelect.setIcon(icon)
        self.actionComment.setIconText("Comment")
        self.actionComment.setObjectName("actionComment")
        self.actionComment.setCheckable(True)
        self.navGroup.addAction(self.actionComment)

        self.toolBar.addAction(self.actionNew)
        self.toolBar.addAction(self.actionOpen)
        self.toolBar.addAction(self.actionSave)
        self.toolBar.addAction(self.actionSaveAs)

        self.toolBar.addAction(self.actionConfigure)
        self.toolBar.addAction(self.actionApply)
        self.toolBar.addAction(self.actionReset)
        self.toolBar.insertSeparator(self.actionConfigure)

        self.toolBar.addAction(self.actionHome)
        self.toolBar.addAction(self.actionPan)
        self.toolBar.addAction(self.actionSelect)
        self.toolBar.addAction(self.actionComment)
        self.toolBar.insertSeparator(self.actionHome)

        self.source_model = build_model()
        self.source_search = QtGui.QLineEdit()
        self.source_search.setPlaceholderText('Search Sources...')
        self.source_tree = build_tree(self.source_model)

        self.node_model = build_model()
        self.node_search = QtGui.QLineEdit()
        self.node_search.setPlaceholderText('Search Operations...')
        self.node_tree = build_tree(self.node_model)

        # subgraphs
        self.subgraph_dock = dockarea.Dock('subgraph', size=(1, 2))
        self.subgraph_dock.nStyle = ""
        self.subgraph_dock.hideTitleBar()
        self.subgraph_toolbar = QtWidgets.QToolBar(parent)
        self.graphGroup = QtWidgets.QActionGroup(parent)

        self.rootGraph = QtWidgets.QAction(parent)
        self.rootGraph.setIconText("root")
        self.rootGraph.setObjectName("rootGraph")
        self.rootGraph.setCheckable(True)
        self.rootGraph.setChecked(True)
        self.graphGroup.addAction(self.rootGraph)
        self.subgraph_toolbar.addAction(self.rootGraph)
        self.subgraph_dock.addWidget(self.subgraph_toolbar, 0, 0, 1, -1)
        chart.addDock(self.subgraph_dock, 'top')

        self.gridLayout.addWidget(self.toolBar, 0, 0, 1, -1)

        self.node_dock = dockarea.Dock('nodes', size=(300, 1000))
        self.node_dock.hideTitleBar()
        self.node_dock.setOrientation('vertical')
        self.node_dock.addWidget(self.source_search, 1, 0, 1, 1)
        self.node_dock.addWidget(self.source_tree, 2, 0, 1, 1)
        self.node_dock.addWidget(self.node_search, 3, 0, 1, 1)
        self.node_dock.addWidget(self.node_tree, 4, 0, 1, 1)
        chart.addDock(self.node_dock, 'left')

        self.gridLayout.addWidget(chart, 1, 1, -1, -1)
        self.gridLayout.setRowStretch(1, 10)
        self.gridLayout.setColumnStretch(1, 10)

        self.node_search.textChanged.connect(self.node_search_text_changed)
        self.source_search.textChanged.connect(self.source_search_text_changed)

    def populate_tree(self, children, parent):
        for child in sorted(children):
            if type(children[child]) is str:
                node = QtGui.QStandardItem(child)
                node.setToolTip(children[child])
                recurse = False
            else:
                recurse = True
                node = QtGui.QStandardItem(child)
            parent.appendRow(node)

            if recurse:
                self.populate_tree(children[child], node)

    def create_model(self, tree, data):
        model = tree.model().sourceModel()
        self.populate_tree(data, model.invisibleRootItem())
        tree.sortByColumn(0, QtCore.Qt.AscendingOrder)
        tree.expandAll()

    def clear_model(self, tree):
        model = tree.model().sourceModel()
        model.clear()

    def node_search_text_changed(self):
        self.search_text_changed(self.node_tree, self.node_model, self.node_search.text())

    def source_search_text_changed(self):
        self.search_text_changed(self.source_tree, self.source_model, self.source_search.text())

    def search_text_changed(self, tree, model, text):
        model.setFilterRegExp(text)
        tree.expandAll()
