import sys
from PyQt6 import QtWidgets, QtCore, QtGui

class TestWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Test Window")
        self.setGeometry(100, 100, 800, 600)
        
        # Create a simple widget
        central_widget = QtWidgets.QWidget()
        self.setCentralWidget(central_widget)
        
        # Add a simple layout with a label
        layout = QtWidgets.QVBoxLayout(central_widget)
        label = QtWidgets.QLabel("Test Window - If you can see this, Qt is working!")
        label.setStyleSheet("font-size: 24px; color: white; background-color: black;")
        layout.addWidget(label)
        
        # Set a simple dark background
        self.setStyleSheet("QMainWindow { background-color: #2b2b2b; }")

def main():
    app = QtWidgets.QApplication(sys.argv)
    window = TestWindow()
    window.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
