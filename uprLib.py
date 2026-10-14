#!/usr/bin/env python
import sys
import os
import pprint
import copy
import subprocess
import fnmatch
import functools
from collections import deque
from typing import Any

from PyQt5.QtWidgets import QWidget
from PyQt5.QtWidgets import QDesktopWidget
from PyQt5.QtWidgets import QApplication
from PyQt5.QtWidgets import QShortcut
from PyQt5.QtWidgets import QTreeView
from PyQt5.QtWidgets import QVBoxLayout
from PyQt5.QtWidgets import QHBoxLayout
from PyQt5.QtWidgets import QGroupBox
from PyQt5.QtWidgets import QPushButton
from PyQt5.QtWidgets import QCheckBox
from PyQt5.QtWidgets import QLineEdit
from PyQt5.QtWidgets import QAbstractItemView
from PyQt5.QtWidgets import QAction
from PyQt5.QtWidgets import QProgressDialog
from PyQt5.QtWidgets import QMessageBox
from PyQt5.QtWidgets import QSizePolicy

from PyQt5.QtGui import QKeySequence
from PyQt5.QtGui import QPalette
from PyQt5.QtGui import QColor
from PyQt5.QtGui import QClipboard
from PyQt5.QtGui import QStandardItem
from PyQt5.QtGui import QIcon

from PyQt5.QtCore import Qt
from PyQt5.QtCore import QThread
from PyQt5.QtCore import QModelIndex
from PyQt5 import QtGui

from uprLibrary import *
import logspinner


def getFullPathUsingFilename(find_filename, full_paths) -> Any | None:
    for full_path in full_paths:
        path_filename = os.path.basename( full_path )
        if path_filename == find_filename :
            return full_path
    return None


class UPRLibWindow(QWidget):
    """Class for the main uprLib window"""

    # noinspection PyUnresolvedReferences
    def __init__(self, upr_lib_val, data_val, app_val, verbose=False):
        """UPRLibWindow constructor.
        
        Args:
            upr_lib_val (UPRLibrary):
                The UPRLibrary object containing the full data that was read from the UPR files.
            data_val (dict):
                A munged form of the data for displaying in the QTreeView
                
            verbose (int):
                When nonzero, more messages will be printed, at increasing amounts with higher values.
        
        Returns:
            Nothing explicitly, implicitly: the constructed object.
        """

        # noinspection PyArgumentList
        super(UPRLibWindow, self).__init__()
        
        self.verbose=verbose
        self.app = app_val
        
        # Add Ctrl+Q hotkey to quit (Alt+F4 still works, but I prefer Ctrl+Q personally)
        quit_shortcut_keyseq = QKeySequence(Qt.CTRL + Qt.Key_Q)
        self.quit_shortcut = QShortcut(quit_shortcut_keyseq, self)
        self.quit_shortcut.activated.connect(self.quitButtonClicked)
        
        copy_shortcut_keyseq = QKeySequence(Qt.CTRL + Qt.Key_C)
        self.copy_shortcut = QShortcut(copy_shortcut_keyseq, self)
        self.copy_shortcut.activated.connect(self.copyToClipboard)
        
        openimage_shortcut_keyseq = QKeySequence(Qt.CTRL + Qt.Key_R)
        self.openimage_shortcut = QShortcut(openimage_shortcut_keyseq, self)
        self.openimage_shortcut.activated.connect(self.openImages)
        
        self.tree = QTreeView(self)
        self.tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tree.setContextMenuPolicy(Qt.ActionsContextMenu)
        
        #self.tree.setContextMenuPolicy(Qt.DefaultContextMenu)
        
        copy_frac_name_acion = QAction("Copy Fractal Name(s)",self)
        copy_frac_name_acion.triggered.connect(self.copySelectedFractalNames)
        self.tree.addAction(copy_frac_name_acion)
        
        treefont = self.tree.font()
        treefont.setPointSize(8)
        self.tree.setFont(treefont)
        self.tree.setUniformRowHeights(True)
        layout = QVBoxLayout(self)
                
        self.top_group = QGroupBox(self)
        self.top_group_layout = QHBoxLayout()
        
        self.top_filter = QLineEdit(self)
        self.top_filter.setEnabled(True)
        self.top_filter.setPlaceholderText("Enter filter string here...")
        #self.top_filter.setText("")
        self.previous_filter_str = ""
        self.top_filter.editingFinished.connect(self.updateList)
        self.top_filter.setToolTip("Filter to use on the list when filtering is enabled.")
        self.top_group_layout.addWidget(self.top_filter)
        
        self.filter_checkbox = QCheckBox(self)
        self.filter_checkbox.setText("Enable Filter")
        self.filter_checkbox.setCheckState(2)
        self.filter_checkbox.toggled.connect(self.toggleFilter)
        self.filter_checkbox.setToolTip("Enable filtering of the tree.")
        self.top_group_layout.addWidget(self.filter_checkbox)
        
        self.casesensitive_checkbox = QCheckBox(self)
        self.casesensitive_checkbox.setText("Case Sensitive")
        self.casesensitive_checkbox.setCheckState(False)
        self.casesensitive_checkbox.toggled.connect(self.updateList)
        self.casesensitive_checkbox.setToolTip("Make the filtering case sensitive.")
        self.top_group_layout.addWidget(self.casesensitive_checkbox)
        
        self.reverse_checkbox = QCheckBox(self)
        self.reverse_checkbox.setText("Reverse")
        self.reverse_checkbox.setCheckState(0)
        self.reverse_checkbox.toggled.connect(self.updateList)
        self.reverse_checkbox.setToolTip("Reverse the tree.")
        self.top_group_layout.addWidget(self.reverse_checkbox)
        
        self.sort_checkbox = QCheckBox(self)
        self.sort_checkbox.setText("Sort")
        self.sort_checkbox.setCheckState(2)
        self.sort_checkbox.toggled.connect(self.updateList)
        self.sort_checkbox.setToolTip("Sort the tree alphabetically.")
        self.top_group_layout.addWidget(self.sort_checkbox)
        
        self.expand_checkbox = QCheckBox(self)
        self.expand_checkbox.setText("Expand")
        self.expand_checkbox.setCheckState(2)
        update_list_with_full_refresh = functools.partial( self.updateList, save_and_restore_expanded_state=False )
        self.expand_checkbox.toggled.connect(update_list_with_full_refresh)
        self.expand_checkbox.setToolTip("Pre-expand the tree so that we see all entries.")
        self.top_group_layout.addWidget(self.expand_checkbox)
        
        self.top_group.setLayout(self.top_group_layout)

        layout.addWidget(self.top_group)
        
        layout.addWidget(self.tree)
        
        #button_layout = QVBoxLayout(self)
        
        self.copy_button = QPushButton(self)
        self.copy_button.setText("Copy UPR to Clipboard")
        self.copy_button.clicked.connect(self.copyToClipboard)
        self.copy_button.setToolTip("Copy the currently selected UPR,\n"
                                    "If multiple are selected you will get the\n"
                                    "last one that was selected.")
        layout.addWidget(self.copy_button)
        
        copy_upr_acion = QAction("Copy Upr",self)
        copy_upr_acion.triggered.connect(self.copyToClipboard)
        self.tree.addAction(copy_upr_acion)
        
        self.openimage_button = QPushButton(self)
        self.openimage_button.setText("Open image")
        self.openimage_button.clicked.connect(self.openImages)
        self.openimage_button.setToolTip("Open the pre-rendered images for the selected UPRs if they can be found.")
        layout.addWidget(self.openimage_button)
        
        open_image_acion = QAction("Open Images",self)
        open_image_acion.triggered.connect(self.openImages)
        # Tooltip did not seem to work with these
        # open_image_acion.setToolTip("Open the pre-rendered images for the selected UPRs if they can be found.")
        self.tree.addAction(open_image_acion)
        
        self.quit_button = QPushButton(self)
        self.quit_button.setText("Quit")
        self.quit_button.clicked.connect(self.quitButtonClicked)
        layout.addWidget(self.quit_button)

        self.model = QtGui.QStandardItemModel()
        self.model.setHorizontalHeaderLabels(["Icon", "Fractal Name","UPR File", "Index In file"])
        self.tree.header().setDefaultSectionSize(180)
        self.tree.setModel(self.model)
        # Method below!
        if self.verbose:
            print("UPRLibWindow init.. setting data..")
            
        self.data=data_val
        self.setDataInTreeWidget(data_val)
        self.upr_lib = upr_lib_val
        self.updateList(save_and_restore_expanded_state=False)

    def displayMessage(self, 
                       dialog_title, 
                       dialog_text, 
                       detailed_text=None, 
                       button_texts=None, 
                       button_roles=None):
                       
        msg_box = QMessageBox(self)
        msg_box.setWindowTitle(dialog_title)
        msg_box.setWindowModality(Qt.WindowModal)
        if button_texts :
            for button_text, button_role in zip(button_texts, button_roles):
                _a_btn = msg_box.addButton(button_text, button_role)
            
        #ok_btn = msg_box.addButton("OK", QMessageBox.AcceptRole)
        #cancel_btn = msg_box.addButton("Cancel", QMessageBox.RejectRole)
        
        msg_box.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        
        msg_box.setText(dialog_text)
        msg_box.setDetailedText(detailed_text)
        
        # This didn't help to enable resizing:
        # I think I might need to swap back to QDialog!
        #msg_box.setMaximumSize(16777215, 16777215)
        #msg_box.setProperty('resizable', True)
        #msg_box.setSizeGripEnabled(True)
        
        #layout = QVBoxLayout()
        #message = QLabel(dialog_text)
        #layout.addWidget(message)
        
        #QBtn = QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        #self.buttonBox = QDialogButtonBox(QBtn)
        #self.buttonBox.accepted.connect(self.dialogAccept)
        #self.buttonBox.rejected.connect(self.reject)
        #layout.addWidget(self.buttonBox)
        
        #msg_box.setLayout(layout)
        #msg_box.exec()
        msg_box.exec_()
        return msg_box
    
    @staticmethod
    def dialogAccept():
        print("Dialog accept!")
    
    def copySelectedFractalNames(self):
        print("copySelectedFractalNames yo!")
        columns_each_row = self.getSelectedAllColumns()
        # can we sort this by index?!
        upr_names = "\n".join( [ str(col[1]) for col in columns_each_row ] )
        self.setClipboard( upr_names )
            
    
    def toggleFilter(self):
        checkbox_state = self.filter_checkbox.checkState()
        # Looks like checkbox_state is 2 when checked, 0 when unchecked.
        
        if checkbox_state:
            self.top_filter.setEnabled(True)
            if self.previous_filter_str:
                self.top_filter.setText(self.previous_filter_str)
            self.updateList()
        else:
            # Don't really want to have to clear this on disable.. 
            # but can't work out how to grey out the field.
            self.previous_filter_str = self.top_filter.text()
            self.top_filter.setText("")
            self.top_filter.setDisabled(True)
            self.updateList()
            
        #self.top_filter.setEnabled
    def getExpandedStates(self, treeview, parent_index=None):
        all_data = {}
        if parent_index is None:
            parent_index = QModelIndex()
        model = treeview.model()
        num_rows = model.rowCount(parent_index)
        for row in range(num_rows):
            index = model.index(row, 0, parent_index)
            name_data = model.data(index)
            
            file_index = model.index(row, 1, parent_index)
            index_index = model.index(row, 2, parent_index)
            file_data = model.data(file_index)
            index_data = model.data(index_index)
            expanded = treeview.isExpanded(index)
            print(f"Got expanded: {expanded}")
            all_data[ (name_data, file_data, index_data) ] = expanded
            
            if model.hasChildren(index):
                all_data.update(self.getExpandedStates(treeview, parent_index=index))
        return all_data
                
    def restoreExpandedStates(self, treeview, previous_states, parent_index=None):
        if parent_index is None:
            parent_index = QModelIndex()
        model = treeview.model()
        num_rows = model.rowCount(parent_index)
        for row in range(num_rows):
            index = model.index(row, 0, parent_index)
            name_data = model.data(index)
            file_index = model.index(row, 1, parent_index)
            index_index = model.index(row, 2, parent_index)
            file_data = model.data(file_index)
            index_data = model.data(index_index)
            # expanded = treeview.isExpanded(index)
            key_tuple = (name_data, file_data, index_data)
            #print(f"Made key_tuple: {key_tuple}")
            if key_tuple in previous_states:
                stored_expanded_state = previous_states[key_tuple]
                print(f"Got stored_expanded_state: {stored_expanded_state}")
                if stored_expanded_state:
                    #print(f"Expanding index: {index}")
                    treeview.expand(index)
                else:
                    #print(f"Collapsing index: {index}")
                    treeview.collapse(index)
            else:
                print(f"did not find key_tuple in previous_states!: {key_tuple}")
            if model.hasChildren(index):
                self.restoreExpandedStates(treeview, previous_states, parent_index=index)
    
    def updateList(self, save_and_restore_expanded_state=True):
        
        previous_expanded_states = {}
        if save_and_restore_expanded_state:
            previous_expanded_states = self.getExpandedStates(self.tree)
                
        datacopy = copy.copy(self.data)
        
        do_sort = self.sort_checkbox.checkState()
        
        case_sensitive = self.casesensitive_checkbox.checkState()
        
        if do_sort:
            # print("Sorting by fractal name!..")
            if case_sensitive:
                datacopy = dict(sorted(list(datacopy.items()), key=lambda itemj: itemj[0].split(":")[-2]))
            else:
                datacopy = dict(sorted(list(datacopy.items()), key=lambda itemu: itemu[0].split(":")[-2].lower()))
            # print("\n".join( datacopy.keys()))
        
        do_reverse = self.reverse_checkbox.checkState()
        if do_reverse:
            # Should this do the reversal recursively?!
            datacopy = dict( reversed( list( datacopy.items() ) ) )
        
        do_filter = self.filter_checkbox.checkState()
        
        # Now filter the data, maybe this also needs to be recursive!?
        if do_filter :
            filter_str = self.top_filter.text()
            filtered_data = {}
            
            for item,val in datacopy.items():
                use_filter_str = filter_str if case_sensitive else filter_str.lower()
                item_fracname = item.split(":")[-2]
                use_item = item_fracname if case_sensitive else item_fracname.lower()
                
                # Make it work like substring match .. always allow stuff on the ends!
                if not use_filter_str:
                    use_filter_str = "*"
                else:
                    if not use_filter_str.startswith("*"):
                        use_filter_str = "*" + use_filter_str
                    if not use_filter_str.endswith("*"):
                        use_filter_str = use_filter_str + "*"
                
                filter_str_re = fnmatch.translate(use_filter_str)

                if re.match( filter_str_re, use_item ):
                    filtered_data[item] = val
                    
            datacopy = filtered_data

        self.setDataInTreeWidget(datacopy)
        self.tree.resizeColumnToContents(0)
        
        auto_expand = self.expand_checkbox.checkState()
        if auto_expand:
            self.tree.expandAll()
        else:
            self.tree.collapseAll()
        
        if save_and_restore_expanded_state:
            self.restoreExpandedStates(self.tree,previous_expanded_states)

    def setClipboard(self, with_text):
        # Need to add name and curly braces!
        text_str = with_text#.encode()
        clipboard = self.app.clipboard()
        clipboard.setText(text_str, QClipboard.Clipboard)

        if clipboard.supportsSelection():
            clipboard.setText(text_str, QClipboard.Selection)
        
        #QThread.msleep(self,1)
    
    @staticmethod
    def addFractalName(fractal_upr, fractal_name):
        return fractal_name + " {\n" + fractal_upr + "\n}"
                
    @staticmethod
    def getAllParents(index):
        # print("\ngetAllParents starting..")
        indices = []
        nindex = index
        
        while nindex.isValid():
            indices.append( nindex.row() )
            nindex = nindex.parent()
            #print("nindex parent is: " + str(nindex))
            #print("nindex parent isValid: " + str(nindex.isValid()))
            #print("nindex parent row: " + str(nindex.row()))
        return list( reversed( indices ) )
        
    def getItemInfo(self, parent_indices):
        # print("getItemInfo, parent indices: " +str(parent_indices))
        
        if not parent_indices:
            return None
        
        anitem = None
        final_pix = 0
        for iqx,pix in enumerate(parent_indices) :
            if iqx == 0:
                anitem = self.model.item( pix )
                if not anitem:
                    # print("No root item: " + str(pix) + " from ")
                    return None
                final_pix = pix
            else :
                parent = anitem
                # Double-safe? probably too much! :P
                if anitem and pix < anitem.rowCount() :
                    anitem = parent.child(pix)
                    final_pix = pix

        cols = self.getColumnsForItem( anitem, final_pix )

        return cols
    
    def getColumnsForItem(self, anitem, pix):
        item_parent = anitem.parent()
        if not item_parent:
            item_parent = self.model.invisibleRootItem()
        columns = []
        for col in range(item_parent.columnCount()):
            columns.append(item_parent.child(pix, col).text())
        return columns
    
    def getSelectedAllColumns(self):
        all_columns = []
        for index in self.tree.selectionModel().selectedRows():
            if index.column() > 0 :
                print("\nSkip data: " + str(index.data()))
                print("Skipping nonzero column callback: " + str(index.column()) + "\n")
                continue
            parents = self.getAllParents(index)
            found_item_cols = self.getItemInfo( parents )
            all_columns.append( found_item_cols )
            
        return all_columns
    
    def openImages(self):
        print("Start of openImages")
        images = []
        columns_each_row = self.getSelectedAllColumns()
        missing_images = []
        
        open_progress = QProgressDialog()
        open_progress.setLabelText("Finding image files..")
        open_progress.setRange(0,len(columns_each_row))
        open_progress.setWindowModality(Qt.WindowModal)
        open_progress.setAutoClose(True)
        open_progress.forceShow() #exec_()
        
        print("\n")
        print(self.data.keys())
        print("\n")
        
        for ixx,found_item_cols in enumerate(columns_each_row):
            print(f"Iterating Row: {ixx}")
            open_progress.setValue( ixx+1 )
            
            if open_progress.wasCanceled():
                return

            print(f"Looking for {found_item_cols[1]} in self.data")
            for key, val in self.data.items():
                if key.split(":")[1] == found_item_cols[1]:
                    print(f"FOUND MATCHING data key {key}")# : {val}")
                    print(f"  {val=}")

                    if 'image_path' in val :
                        images.append( val['image_path'] )
                        print(f"  appended image {val['image_path']}")

                else:
                    continue
            
            if False : # Old globbing aproach!
                #print("\n")
                #print("openImages got cols: " + str(found_item_cols))
                config_image_root = self.upr_lib.config_dict.get('upr_image_root_dir')

                if not config_image_root:
                    return

                #print("Got Image Root from config: " + str(config_image_root))
                glob_for = found_item_cols[1] + ".tif"

                #print("Globbing for: " + str( glob_for ))
                #print(".. in dir " + str( config_image_root ))

                globbed_image = glob.glob("**/" + glob_for, 
                                          root_dir=config_image_root,
                                          recursive=True)

                if globbed_image:
                    print("Found globbed image: " + str( globbed_image ))
                    # Could be multiple matches I guess!? take first for now.
                    image_fullpath = os.path.join( config_image_root, globbed_image[0] )
                    print("Globbed image fullpath: " + str( image_fullpath ))
                    images.append( image_fullpath )
                else:
                    print("No image found for: " + str( glob_for ))
                    missing_images.append( found_item_cols[1] )
        
        if not images :
            self.displayMessage("No Images Found", 
                                "No images were found for any of the\nselected fractals I'm afraid!",
                                None)
            return
        
        if len(images) < len(columns_each_row):
            msg = "We only found images for " + str(len(images)) + " out of " + str(len(columns_each_row)) + " selected fractals!"
            
            msg_dtl = "Missing:\n  "
            msg_dtl += "\n  ".join(missing_images)
            btn_labels = ["Open Images Anyway","Cancel"]
            btn_roles = [ QMessageBox.AcceptRole, QMessageBox.RejectRole ]
            msg_box = self.displayMessage("Some Images Were Not Found", 
                                          msg,
                                          msg_dtl,
                                          btn_labels,
                                          btn_roles,)
            clicked_btn = msg_box.clickedButton()
            button_role = msg_box.buttonRole( clicked_btn )
            print("Got button_role: " + str( button_role ))
            if button_role == QMessageBox.RejectRole :
                return
           
        image_cmd = [ "gwenview", "-f", "-s", ]
        image_cmd.extend(images)
        image_cmd.append("&")
        
        _pope = subprocess.Popen( image_cmd, stdout=subprocess.PIPE )
        # lines = pope.stdout.readlines()
        
    def copyToClipboard(self):
        print("Copy UPR Clicked!")
        
        # TODO: copy-to-clipboard
        
        #basesel = self.tree.model.selectedIndexes()
        #for bs in basesel:
        #    print("bs:" + str(bs))
        
        # print("\n".join(dir(self.tree)))
        # print("\n".join(dir(self.model)))
                
        #for row in self.tree.selectionModel().selectedRows():
        #    print("Row yo!: " + str(row))
        #    print("Row text!: " + str(row.text()))
        
        #for index in self.tree.selectedIndexes():
        for index in self.tree.selectionModel().selectedRows():
        
            if index.column() > 0 :
                print("\nSkip data: " + str(index.data()))
                print("Skipping nonzero column callback for copy: " + str(index.column()) + "\n")
                continue
                
            print("\n" +
                  "   selected row: " + str(index.row()))
            print("         column: " + str(index.column()))
            print("           data: " + str(index.data()))
            print("     internalId: " + str(index.internalId()))
            print("          flags: " + str(index.flags()))
            print("           data: " + str(index.data()))
            print("          model: " + str(index.model()))
            print("      raw index: " + str(index))
            
            parents = self.getAllParents(index)
            print("        parents: " + str(parents))
            
            #testitem = self.tree.model().itemFromIndex( index )
            #print("Got testitem: " + str(testitem))

            # Sounds like some kind of mapping is needed here?!
            #source_index = self.model.mapToSource(index)
            found_item_cols = self.getItemInfo( parents )
            
            print("found item cols: " + str(found_item_cols))

            all_uprs_dict = self.upr_lib.getAllUPRs()
            
            # Eventually want to do this differently in case there are
            # more than one file with the same filename in different paths
            # but this is ok for the moment!
            selected_upr_name = found_item_cols[1]
            selected_filename = found_item_cols[2]
            selected_index = int(found_item_cols[3])
            upr_name_key = selected_upr_name + ":" + str(selected_index)
            full_upr_path = getFullPathUsingFilename(selected_filename,all_uprs_dict.keys())
            
            if full_upr_path not in all_uprs_dict:
                print("ERROR: Upr file was not found in library! This is most odd! Bailing out.")
                return
                
            print("Found upr file in UPR lib!")
            
            upr_data_dict = all_uprs_dict[full_upr_path]
            
            if upr_name_key in upr_data_dict:
                if self.verbose:
                    print("\nKey found!\n")
                # This is a string.. the actual fractal code in plaintext UPR
                upr_data = upr_data_dict[upr_name_key]
                fractal_with_name = self.addFractalName(upr_data, selected_upr_name)
                self.setClipboard( fractal_with_name )
            
            print( "\n" )

    def quitButtonClicked(self, by_hotkey=False):
        if self.verbose:
            if not by_hotkey:
                print("\nQuit button pressed! Exiting.\n")
            else:
                print("\nQuit-by-hotkey callback, quitting!\n")
        sys.exit(0)
        
    def setDataInTreeWidget(self, data_val, root=None):
        if self.verbose:
            print("setDataInTreeWidget start..")
        
        self.model.setRowCount(0)
        if root is None:
            root = self.model.invisibleRootItem()
            
        seen = {}
        
        values = deque(data_val.values())
        if self.verbose > 4:
            print("values: " + str(data_val))
            
        while values:
            value = values.popleft()
            
            if value['level'] == 0:
                parent = root
            else:
                pid = value['parent_id']
                if pid not in seen:
                    values.append(value)
                    continue
                parent = seen[pid]
                
            ijx = value['ix']
            
            upr_filex = value['upr_file']
            
            #print( "Appendrow!" + str( value ) )
            # Better be no other colons in there eh!!?
            upr_name_no_ix, upr_name_ix = value['upr_name'].split(":")
            
            icon_item = QStandardItem()
            # This worked! however we currently have to scroll to the right to see them!
            if 'thumb_path' in value :
                thumb_filex = value['thumb_path']
                #print(f"{thumb_filex=}")
                #test_png_path = "/home/dan/quid/ultraFields/.thumb.baseBlue_doublebasin_crnr_moreIterations.png"
                icon_item.setData(QIcon(thumb_filex), Qt.ItemDataRole.DecorationRole)
        
            parent.appendRow([
                icon_item,
                QtGui.QStandardItem( upr_name_no_ix ),
                QtGui.QStandardItem( os.path.basename(upr_filex) ),
                QtGui.QStandardItem( upr_name_ix ),
                ])
            seen[ijx] = parent.child(parent.rowCount() - 1)
        if self.verbose:
            print("setDataInTreeWidget finished.")
    
def listsMatchShortest( alist, blist ) :
    runlen = min( len(alist), len(blist) )

    if runlen == 0 :
        return False

    for ixx in range(runlen) :
        if alist[ixx] != blist[ixx] :
            return False
    return True    

def rebuildParentingDataAsDict(data_val, shell_ui_enable=False, verbose=0) :
    # TODO: make this a commandline arg! OFF For now since it's a UI!
    verbose=10
    spinnerj = logspinner.spinning_cursor() if shell_ui_enable else None
    
    # Flatten data, such that structure can
    # be reconstructed at the end.
    flat_data = {}
    _upr_count = 0
    upr_map = {}
    
    for iqx, upr_data in enumerate(data_val) :
        #for upr_data_name in upr_data :
        flatkey = upr_data['upr_file'] + ":" + upr_data['upr_name']
        flat_data[ flatkey ] = upr_data
        flat_data[ flatkey ]['ix'] = iqx

        # The point here is to get an index for each file by making a list
        if upr_data['upr_file'] not in upr_map :
            upr_map[ flatkey ] = 1
        
        #print("ix: " + str( ix ) )
        #print("datalen: " + str(len(data)))
        
        percent = iqx / max( (len(data_val) - 1), 1 )
        logspinner.write_spinner(spinnerj,
                                 prefix="Rebuilding fractal indices",
                                 percent=percent)

    # New flat ids: 'ix'!
    if verbose:
        print( "rebuildParentingDataAsDict, Total entries in flat_data: " + str( len( flat_data.keys() ) ) )
    # Make map of all dotdotdots.
    # Find all parents for dotdotdots.
    # Use deque so that if parent is not
    # found we can chuck the entry on the
    # end of the list for re-processing.
    # Modify parent id (a 2-part id now)
    # Fix level.
    ddd = '_dotdotdot_'
    count = 0
    flat_kp = {}

    # Use a deque of tuples here and check whether the parent is
    # included yet, and if not, push the current entry to the end of the queue.
    
    # End the spinner line
    logspinner.endSpinner(spinnerj)
    
    dq = deque( flat_data.items() )
    dqiter_count = 0
    oldq = len(dq)
    
    if verbose > 2:
        print("Original double-ended-queue length: " + str(oldq))
        
    while dq:
        # TODO piss-off all these acronyms! too lazy!
        dqv = dq.popleft()
        upr_key, upr_data = dqv
        ldq = len( dq )
        # percent = 1 / (ldq - 1) if ldq > 1 else 1.0
        # This whole extendable-queue thing seems to be hard to make a
        # good percentage for.. but I noticed that we most often end up 
        # with less actual count than the original list had entries..
        # so trying this, dividing the current count by the original length
        # Guess it will never reach 100% unl;ess we do something about it..
        # so this should swap to 100% on the last iter..
        percent = dqiter_count / oldq if ldq > 1 else 1.0
        logspinner.write_spinner(spinnerj,
                                 prefix="Preparing Fractal Tree",
                                 percent=percent)
        #print("percent: " + str(percent))
        #sys.stdout.flush()
        
        #if verbose > 3:
        # sys.stdout.write('Processing ... ' + str(upr_key) + '\n')
        
        for uijx,upr_data_name in enumerate(upr_data):
            # Check for len zero!? no need, won't have iterated!
            _upercent = uijx / len(upr_data)

            if upr_data_name == 'upr_name':
                upr_name = upr_data[ upr_data_name ]
                
                if True: #not ddd in upr_name :
                    #count += 1
                    
                    # TODO: find if any other upr in whole list has shared prefix and parent to it
                    # .. somehow..  then extend this to the dotdotdot ones as well
                    
                    colonsplit = upr_name.split( ":" )
                    uscoresplit = colonsplit[0].split("_")
                    
                    if verbose > 4:
                        print("got uscoresplit: " + str( uscoresplit ) )
                    
                    # Now check all other fractal names! zomg.. need to speed this approach up!
                    for ixf, fupr_key in enumerate( flat_data.keys() ):
                        frac_name = fupr_key.split(":")[1]
                        # print("frac_name: " + frac_name )
                        frac_name_split = frac_name.split("_")
                        if len( uscoresplit ) > len( frac_name_split ):
                        
                            matched = True
                            
                            for frix,subname in enumerate( frac_name_split ):
                                if subname != uscoresplit[ frix ] :
                                    matched = False
                                    break

                            if not matched:
                                continue
                                
                            fupr_data = flat_data[ fupr_key ]
                            if 'ix' in fupr_data :
                                if verbose > 4:
                                    print( "Matched a parent! setting base-type parent_id to: " + str( fupr_data['ix'] ) )
                                upr_data['parent_id'] = fupr_data['ix']
                                upr_data['level'] = max(1, fupr_data['level'] + 1)
                                flat_kp[upr_key] = upr_data
                                flat_kp[fupr_key] = fupr_data
                                # print( "parent data: " + str( fupr_data ) )
                                #print( "upr data: " + str( upr_data ))
                                
                    # Add it whether it was modified or otherwise!
                    flat_kp[upr_key] = upr_data
                    count += 1
                    
                #for fupr_key in flat_kp:
                #    print("fupr_key:" + str( fupr_key ))

                # Just to fool pyCharm
                temp = False
                if temp: # ddd in upr_name: # Still not working yet!
                    
                    # TODO: make it so that this is an additional reparenting after the first name-based pass!
                    # Currently the tree is flat inside all dotdotdot parents!
                    
                    if verbose > 4 :
                        print("\n\nUPR name contains dotdotdot!: " + upr_name)
                        
                    dotsplit = upr_name.split( ddd )
                    
                    if len( dotsplit ) > 1 :
                        restspl = dotsplit[1].strip("_").split("_")
                        # print( "dotsearch: " + str( restspl[0] ) )
                        
                        # ORDER squared? warning!!! At least there's a bailout!?
                        # This is slow.
                        # Check all other fractal names for this prefix..
                        #
                        for ixf, fupr_key in enumerate( flat_data.keys() ):                            

                            # print( "Checking fupr_key:" + str( fupr_key ) )
                            #print( "Post dotdotdot is: " + str( restspl[0] ) )

                            # Must end with this! (underscore plus the bit before the dotdotdot!)
                            frac_key = "_" + restspl[0]
                            
                            frac_name = fupr_key.split(":")[1]
                            # Extra underscores at start and end be damned!! 
                            # Treat as error and remove.. probably a bug at some level!
                            frac_name_underscore_split = frac_name.strip("_").split("_")
                            
                            # First part(s) up to the dotdotdot need to match too!
                            # get ready to compare lists..
                            
                            pdspl = dotsplit[0].split("_")

                            # Probably need to find the longest match here instead of the
                            # first!!
                            # Conservative for the moment!
                            
                            # This condition says: We have found a fractal that ends with
                            # _example, where 'example' is the first word /after/ ddd: '_dotdotdot_'
                            # in the fractal name that we are searching-for-a-parent for, 
                            # AND (I think) it's not a sibling dotdotdot (will have ddd at minus two)
                            # AND the bit before ddd on both sides is equal.
                            
                            if frac_name.endswith( frac_key ) \
                                and frac_name_underscore_split[-2] != ddd \
                                and listsMatchShortest( pdspl, frac_name_underscore_split ) :
                                
                                if verbose > 4 :
                                    print("Pdspl is: " + str(pdspl) )
                                    print("frac_name_underscore_split is: " + str(frac_name_underscore_split) )
                                    print("frac_key is: " + frac_key)
                                    print("MATCHED parent upr key: " + fupr_key)
                                    
                                #print( "\nThis UPR: " +  upr_name.split(":")[0] )
                                #print("MATCHED parent upr key: " + os.path.basename(frac_name))
                                
                                fupr_data = flat_data[ fupr_key ]
                                if 'ix' in fupr_data :
                                    #print( "setting parent_id to: " + str( fupr_data['ix'] ) )
                                    upr_data['parent_id'] = fupr_data['ix']
                                    upr_data['level'] = max(1, fupr_data['level'] + 1)
                                    flat_kp[upr_key] = upr_data
                                    flat_kp[fupr_key] = fupr_data
                                # print( "parent data: " + str( fupr_data ) )
                                #print( "upr data: " + str( upr_data ))
                                count += 1
                                # early bailout for debug:
                                #if count > 100 :
                                #    return flat_kp
                            else:
                                pass
                                #print("no match!: " + frac_key )
        dqiter_count += 1
        
    # End the spinner line
    logspinner.endSpinner(spinnerj)
    if verbose:
        print("Final count: " + str( count ))
        
    if verbose>5:
        print("\nResulting dict:")
        pprint.pprint(flat_kp,width=40)
        print()
    
    return flat_kp

def addImagePathsToData(data, verbose=False):
    config_dict = UPRLibrary(verbose=verbose).getConfigDict()
    if verbose:
        print(f"{config_dict=}!")
    
    if not 'upr_image_root_glob' in config_dict:
        # TODO: complain!
        return data
    
    # DONE: allowed this to support a glob, so we can have:
    # /home/dan/quid/ultra*/ and ignore everything else that is
    # in that dir.
    image_root_glob = config_dict["upr_image_root_glob"]
    if verbose:
        print(f"\n Image root glob: {image_root_glob}")
    
    upr_image_extensions = config_dict["upr_image_extensions"].split(",")
    #print(f"\n upr_image_extensions: {upr_image_extensions}")
    
    # TODO: learn pathlib!
    dirglob = glob.glob(image_root_glob)
    total_count = 0
    glob_and_thumb_info = {}
    
    for globbed_dir in dirglob :
        if not os.path.isdir(globbed_dir):
            continue
        if verbose:
            print(f"  {globbed_dir}")
        for img_ext in upr_image_extensions:
            # Ignore subdirs for now!
            # eventually.. include specifically 16k/ .. but do that
            # when carefully associating lowres with highres renders of
            # the same thing!
            globbed_files = glob.glob(os.path.join(globbed_dir, f"*.{img_ext}"))
            count = 0
            thumb_count = 0
            for globbed_file in globbed_files :
                #if count > 0 :
                #    print("BAIL at 2!")
                #    break
                
                if not os.path.isfile(globbed_file):
                    if verbose:
                        print( f"Globbed thing was not a file! Skipping: {globbed_file}" )
                    continue
                    
                total_count += 1
                dirpart = os.path.dirname(globbed_file).strip()
                basepart = os.path.basename(globbed_file).strip()
                
                if os.path.basename(basepart).startswith(".thumb"):
                    # Skip any existing .thumb files!
                    continue
                
                # hmm.. magick doesn't really seem too fond of these files unforunately :/
                # magick_cmd = [ "magick",
                
                # TODO: replace ext with png or jpg here!
                thumb_file = os.path.join(dirpart, f".thumb.{basepart}")
                thumb_file_split = list(os.path.splitext(thumb_file))
                thumb_file_split[1] = "png"
                thumb_file = ".".join(thumb_file_split)
                
                if not os.path.isfile(thumb_file):
                    if verbose:
                        print("\n")
                        print(f"    {total_count} : {globbed_file}")                    
                        print(f"No thumb file found, regenerating: {thumb_file}")
                    
                        print( f"Before checking thumb file: {os.path.isfile(thumb_file)=}" )
                        print( f"Before checking thumb file: {glob.glob(thumb_file)=}" )
                
                    ff_cmd = [ "ffmpeg", 
                               "-i", globbed_file, 
                               "-vf", "thumbnail,scale=64:-1",
                               thumb_file ]
                           
                    print( "Running: " + " ".join( ff_cmd ) )
                    pope = subprocess.Popen( ff_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE )
                    status = pope.wait()
                    
                    print( f"Command status was: {status}" )
                    
                    if verbose :
                        print( f"After generating thumb file: {os.path.isfile(thumb_file)=}" )
                        print( f"After generating thumb file: {glob.glob(thumb_file)=}" )
                    thumb_count += 1
                    
                count += 1
                
                if os.path.isfile(thumb_file) :
                    globbed_file_basename = os.path.basename( globbed_file )
                    gf_basename_splitext = os.path.splitext( globbed_file_basename )
                    if verbose :
                        print(f"Recording {gf_basename_splitext[0]=} thumb: {thumb_file}")
                    glob_and_thumb_info[ gf_basename_splitext[0] ] = [ globbed_file, thumb_file ]
    
    for key, val in data.items() :
        if 'upr_name' in val :
            upr_name_with_index = val['upr_name']
            clean_upr_name = upr_name_with_index.split(":")[0]
            
            if verbose:
                print(f"Val has clean_upr_name: {clean_upr_name}")
            
            if clean_upr_name in glob_and_thumb_info :
                if verbose:
                    print(f"Appending image and thumb for {clean_upr_name}")
                # Note!: Adding to a mutable entry here! Slightly dangerous!
                val[ 'image_path' ] = glob_and_thumb_info[ clean_upr_name ][0]
                val[ 'thumb_path' ] = glob_and_thumb_info[ clean_upr_name ][1]
            else :
                if verbose:
                    print(f"Not found in thumbs: {clean_upr_name}")
                
    #print("\n")
    #print(data)
    #print("\n")
    
    return data
    
if __name__ == "__main__":
    # TODO get some options here!
    # verbose
    # limit num uprs
    argparser = argparse.ArgumentParser()
    
    argparser.add_argument("-v", 
                           "--verbose", 
                           help="Turn on verbose-mode.",
                           default=0,
                           action="count" )
                           
    # TODO: eventually allow which upr file to use to be specified/
    # Note: becomes args.num_upr_file_limit (just to help with breadcrumbs)
    argparser.add_argument("-f", 
                           "--num-upr-file-limit", 
                           help="Limit the number of UPR /files/ to load overall.",
                           type=int,
                           default=None,
                           action="store" )
                           
    # TODO: Add the ability to filter/glob or match entries instead of just truncating the list!
    argparser.add_argument("-n", 
                           "--num-upr-limit", 
                           help="Limit the number of UPRs to load from each .upr file.",
                           type=int,
                           default=None,
                           action="store" )
                           
    argparser.add_argument("-c", 
                           "--config_filename", 
                           help="Use specified filename as config, default is 'uprLibrary.config'.",
                           type=str,
                           default=None,
                           action="store" )
                      
    argparser.add_argument("-u", 
                           "--disable-shell-ui", 
                           help="Turn off shell ui like spinners/etc while loading.",
                           default=False,
                           action="store_true" )
                           
    argparser.add_argument("-g", 
                           "--disable-gui", 
                           help="Turn off main ui.. for debugging or when you only want to see shell output.",
                           default=False,
                           action="store_true" )
                           
    args = argparser.parse_args()
    
    shell_ui = not args.disable_shell_ui
    if shell_ui:
        # Print this whether verbose or otherwise when shell_ui is on!
        # Otherwise expectation is complete silence!
        print("\nUprLib starting up..")
    
    if args.verbose:
        print("\nVerbosity has nonzero value: " + str( args.verbose) )
    # home_directory = expanduser('~')
    
    app = QApplication(["UPRLib"])
    
    app.setStyle("Fusion")

    # Now use a palette to switch to dark colors:
    palette = QPalette()
    palette.setColor(QPalette.Window, QColor(53, 53, 53))
    palette.setColor(QPalette.WindowText, Qt.white)
    palette.setColor(QPalette.Base, QColor(25, 25, 25))
    palette.setColor(QPalette.AlternateBase, QColor(53, 53, 53))
    palette.setColor(QPalette.ToolTipBase, Qt.black)
    palette.setColor(QPalette.ToolTipText, Qt.white)
    palette.setColor(QPalette.Text, Qt.white)
    palette.setColor(QPalette.Button, QColor(53, 53, 53))
    palette.setColor(QPalette.ButtonText, Qt.white)
    palette.setColor(QPalette.BrightText, Qt.red)
    palette.setColor(QPalette.Link, QColor(42, 130, 218))
    palette.setColor(QPalette.Highlight, QColor(42, 130, 218))
    palette.setColor(QPalette.HighlightedText, Qt.black)
    # noinspection PyArgumentList
    app.setPalette(palette)
    # QToolTip::setPalette(palette); for the settings to take effect on tooltips
    
    #style_file = QFile(":/dark.qss")
    #style_file.open(QFile.ReadOnly | QFile.Text)
    #stream = QTextStream(style_file)
    #app.setStyleSheet(stream.readAll())
    
    style_hints = app.styleHints()
    
    #print( "\n" + "\n".join( dir( style_hints ) ) + "\n" )
    
    # No property afaict
    #ahp = style_hints.property("QT::ColorScheme")
    #print("ahp!:" + str( ahp ))

    
    # Could pass a config file maybe?
    # new arg: config_filename
    
    upr_lib = UPRLibrary( num_upr_file_limit=args.num_upr_file_limit,
                          num_upr_limit=args.num_upr_limit,
                          config_filename=args.config_filename,
                          shell_ui=shell_ui,
                          verbose=args.verbose )
    
    # Move this into a button-press!
    all_upr = upr_lib.loadAllUpr()
    # curr_upr = upr_lib.getCurrentUpr()
    # print("Current UPR: " + curr_upr)
    # upr_file = UPRFile(curr_upr)
    # print("This UPR file has: " + str(len(upr_file.lines)) + " lines.")
    upr_dict = upr_lib.getAllUPRs()
    # upr_file.getUprDict()
    upr_dict_keys = upr_dict.keys()

    if args.verbose:
        print( "Found " + str( len( upr_dict_keys ) )  + " upr files.")
        
    raw_data = []
    totalix = 0
    
    
    spinner = logspinner.spinning_cursor() if shell_ui else None
    
    for uix, upr_file in enumerate( sorted( upr_dict_keys ) ):
        for ix, uprname in enumerate( upr_dict[ upr_file ].keys() ):
            uprname_clean = uprname.split(":")[0]
            raw_data.append( { 'level':0,
                           'dbid':totalix,
                           'uprid':str(uix)+":"+str(ix),
                           'parent_id':-1,
                           'upr_name': uprname,
                           'upr_file': upr_file } )
            totalix += 1
            
    if args.verbose:
        print("Raw data had " + str( len( raw_data ) )  + " entries..")
    
    # Note, this method also /flattens/ the data to a single dict where
    # previously it was a list of dicts!
    
    data = rebuildParentingDataAsDict(raw_data, shell_ui_enable=shell_ui, verbose=args.verbose)
    
    if args.verbose:
        print("rebuildParentingDataAsDict returned " + str( len( data ) )  + " entries..")
    
    if args.verbose:
        print("\n")
        for key, val in data.items():
            print( f"{key}" )
            for ikey, ival in val.items() :
                print( f"    {ikey} : {ival}" )
    
    data = addImagePathsToData(data, verbose=args.verbose)
    
    data = dict(sorted(list(data.items()) , key=lambda item: item[0]))
    
    if args.disable_gui:
        if args.verbose:
            print("\nDisable GUI arg specified, bailing out instead of showing the UI!\n")
        sys.exit(0)
        
    sdwindow = UPRLibWindow(upr_lib, data, app, verbose=args.verbose)
    
    screen = app.primaryScreen()
    screenGeometry = screen.size()
    # Eventually make this a pref:
    # Make the window as big as the main
    # screen, and centered on it.
    xsize = screenGeometry.width()
    ysize = screenGeometry.height()
    sdwindow.resize( xsize, ysize )
    # Move to the middle
    vgeo = sdwindow.frameGeometry()
    center_point = QDesktopWidget().availableGeometry().center()
    vgeo.moveCenter(center_point)
    sdwindow.move( vgeo.topLeft() )
    sdwindow.show()
    if args.verbose > 1:
        print("Init all done, starting app!")
    app.exec_()
    # Seems like any prints on lines after this might never happen (Even with flush!)
