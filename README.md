# SimCricketX 🏏

SimCricketX is a Python Flask-based web application that simulates T20 cricket matches with ball-by-ball commentary. Users can create custom teams, manage players, and experience realistic match dynamics with live updates and statistics.

---

## 🚀 Features

### 🔄 Authentic Cricket Simulation Engine
- **Realistic Match Dynamics**: Ball-by-ball simulation considering pitch, skills, and context.
- **Full T20 Format**: 20-over matches with bowling restrictions and strategy.
- **Weather Integration**: Rain with simplified DLS rules.
- **Super Over Support**: Auto tie-breaker via super over gameplay.

### 🧢 Comprehensive Team Management
- **Custom Teams**: Create teams with 15–18 players.
- **Detailed Player Profiles**: Batting, bowling, and special skills included.
- **Strategic Setup**: Choose captain, keeper, and playing XI.
- **Visual Branding**: Customize team colors and identity.

### 📢 Immersive Match Experience
- **Interactive Setup**: Coin toss, pitch selection, and team choices.
- **Live Commentary**: Real-time dynamic ball-by-ball updates.
- **Advanced Stats**: Live scorecards, player performance, and match analytics.
- **Viewing Modes**: Switch between scoreboard, summary, and deep stats.

### 📊 Data & Analytics
- **Match Reports**: Auto-generated in HTML, CSV, JSON, and TXT.
- **History & Archive**: Searchable match history and data logs.
- **Performance Insights**: Player and match-level performance tracking.

### 🖥️ Modern UX
- **6 Visual Themes**: Nord, Retro, Cupcake, Dim, Dracula, Sunset.
- **Responsive UI**: Desktop, tablet, and mobile support.
- **Dark/Light Mode**: Auto theme persistence.
- **Intuitive UX**: Drag-and-drop and easy navigation.

---

## 🛠️ Installation

Follow these steps to set up SimCricketX on your local machine:

### Prerequisites
- Python 3.10 or higher
- Git
- A terminal or command prompt

### Steps

1. **Clone the Repository**:
   ```bash
   git clone https://github.com/your-repo/SimCricketX.git
   cd SimCricketX
   ```

2. **Set Up a Virtual Environment**:
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

3. **Install Dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

4. **Set Up Configuration**:
   - Copy the `config/config.yaml` file and update it with your settings if needed.

5. **Run the Application**:
   ```bash
   python app.py
   ```
   The application will be available at `http://127.0.0.1:5000`.

---

## 🧪 Testing

Run the test suite to ensure everything is working correctly:
```bash
python -m unittest discover tests
```

---

## 📂 Project Structure

- **`app.py`**: Main application entry point.
- **`engine/`**: Core simulation logic and services.
- **`templates/`**: HTML templates for the web interface.
- **`static/`**: Static assets (CSS, JS, images).
- **`data/`**: Match data and logs.
- **`tests/`**: Unit tests for the application.

---

## ⚙️ Configuration

You can customize the application behavior by editing the `config/config.yaml` file. Key settings include:
- `secret_key`: Application secret key.
- `database`: Database connection settings.

---

## 📜 License

This project is licensed under the MIT License. See the `LICENSE` file for details.

---

## 🙌 Credits

Built with ❤️ using Flask, HTML/CSS/JS, and cricket fandom.

