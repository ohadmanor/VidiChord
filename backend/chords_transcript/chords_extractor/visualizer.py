import json
import os

def generate_html(json_data, output_path):
    """
    Generates a beautiful HTML visualizer from the chord JSON data.
    Displays results from Librosa, Essentia, Madmom, and the Final fusion.
    """
    metadata = json_data.get('metadata', {})
    beats = json_data.get('beats', [])

    # Group beats by bar
    bars = {}
    for b in beats:
        bar_num = b['bar']
        if bar_num not in bars:
            bars[bar_num] = []
        bars[bar_num].append(b)

    sorted_bars = sorted(bars.items(), key=lambda x: x[0])

    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Chord Transcription: {metadata.get('audio_file', 'Audio')}</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;800&family=Outfit:wght@500;700&display=swap" rel="stylesheet">
    <style>
        :root {{
            --bg-color: #0f172a;
            --text-color: #f8fafc;
            --card-bg: rgba(30, 41, 59, 0.7);
            --border-color: rgba(148, 163, 184, 0.1);
            --accent-primary: #3b82f6;
            --accent-secondary: #8b5cf6;
            --chord-color: #60a5fa;
            --n-chord-color: #475569;
            --repeated-chord: transparent;
            --active-glow: rgba(59, 130, 246, 0.5);
        }}

        body {{
            font-family: 'Inter', sans-serif;
            background-color: var(--bg-color);
            color: var(--text-color);
            margin: 0;
            padding: 2rem;
            min-height: 100vh;
            background-image: radial-gradient(circle at 10% 20%, rgba(59, 130, 246, 0.15) 0%, transparent 20%),
                              radial-gradient(circle at 90% 80%, rgba(139, 92, 246, 0.15) 0%, transparent 20%);
            background-attachment: fixed;
        }}

        .container {{
            max-width: 1200px;
            margin: 0 auto;
            padding-bottom: 100px; /* Space for sticky audio player */
        }}

        header {{
            text-align: center;
            margin-bottom: 2rem;
        }}

        h1 {{
            font-family: 'Outfit', sans-serif;
            font-size: 3rem;
            margin: 0 0 1rem 0;
            background: linear-gradient(to right, var(--accent-primary), var(--accent-secondary));
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }}

        .metadata {{
            display: flex;
            justify-content: center;
            gap: 1.5rem;
            flex-wrap: wrap;
            margin-bottom: 2rem;
        }}

        .badge {{
            background: var(--card-bg);
            padding: 0.75rem 1.5rem;
            border-radius: 9999px;
            border: 1px solid var(--border-color);
            backdrop-filter: blur(10px);
            font-weight: 600;
            font-size: 0.9rem;
        }}
        .badge span {{
            color: var(--accent-primary);
            margin-right: 0.5rem;
        }}

        /* Sticky Audio Player */
        .audio-container {{
            position: sticky;
            top: 1rem;
            z-index: 100;
            background: rgba(15, 23, 42, 0.85);
            backdrop-filter: blur(12px);
            padding: 1rem;
            border-radius: 1rem;
            border: 1px solid rgba(59, 130, 246, 0.3);
            box-shadow: 0 10px 25px -5px rgba(0, 0, 0, 0.5);
            margin-bottom: 2rem;
            display: flex;
            justify-content: center;
        }}

        audio {{
            width: 100%;
            max-width: 600px;
            outline: none;
        }}

        /* Grid */
        .grid-container {{
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(400px, 1fr));
            gap: 1.5rem;
        }}

        .bar-card {{
            background: var(--card-bg);
            border-radius: 1rem;
            border: 1px solid var(--border-color);
            padding: 1.5rem;
            backdrop-filter: blur(10px);
            box-shadow: 0 10px 15px -3px rgba(0, 0, 0, 0.1);
            transition: all 0.3s ease;
            cursor: pointer;
        }}

        .bar-card:hover {{
            transform: translateY(-2px);
            border-color: rgba(139, 92, 246, 0.3);
            box-shadow: 0 15px 25px -5px rgba(0, 0, 0, 0.2);
        }}

        /* Active states */
        .bar-card.active-bar {{
            border-color: var(--accent-primary);
            box-shadow: 0 0 20px rgba(59, 130, 246, 0.4);
            transform: scale(1.02);
            background: rgba(30, 41, 59, 0.9);
        }}

        .bar-header {{
            font-family: 'Outfit', sans-serif;
            font-size: 1.25rem;
            color: var(--accent-secondary);
            margin-bottom: 1rem;
            border-bottom: 1px solid var(--border-color);
            padding-bottom: 0.5rem;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }}

        .bar-time {{
            font-size: 0.8rem;
            color: #94a3b8;
            font-family: 'Inter', sans-serif;
        }}

        table.bar-table {{
            width: 100%;
            border-collapse: collapse;
        }}

        table.bar-table th, table.bar-table td {{
            padding: 0.5rem;
            text-align: center;
            border-bottom: 1px solid var(--border-color);
            transition: background-color 0.2s;
        }}

        table.bar-table th {{
            color: #94a3b8;
            font-size: 0.8rem;
            font-weight: 600;
            text-transform: uppercase;
        }}

        table.bar-table td {{
            font-size: 1.1rem;
            font-weight: 700;
        }}

        table.bar-table tr:last-child td {{
            border-bottom: none;
        }}

        .model-name {{
            text-align: left !important;
            color: var(--accent-secondary);
            font-size: 0.9rem !important;
        }}

        .chord-val {{
            color: var(--chord-color);
        }}

        .chord-val.none {{
            color: var(--n-chord-color);
            font-weight: 500;
        }}

        .chord-val.repeated {{
            color: var(--repeated-chord);
            user-select: none;
        }}
        
        .chord-val.repeated::after {{
            content: '-';
            color: var(--n-chord-color);
            opacity: 0.5;
        }}

        .final-row {{
            background: rgba(59, 130, 246, 0.05);
        }}
        .final-row .model-name {{
            color: var(--accent-primary);
            font-weight: 800;
        }}

        /* Beat active highlight */
        .active-beat {{
            background-color: rgba(59, 130, 246, 0.2);
            border-radius: 4px;
        }}
        
        th.active-beat {{
            color: #fff;
        }}
    </style>
</head>
<body>
    <div class="container">
        <header>
            <h1>{metadata.get('audio_file', 'Audio File')}</h1>
            <div class="metadata">
                <div class="badge"><span>BPM</span>{metadata.get('bpm', 'N/A')}</div>
                <div class="badge"><span>Key</span>{metadata.get('estimated_key', 'N/A')}</div>
                <div class="badge"><span>Duration</span>{metadata.get('duration', 0)}s</div>
                <div class="badge"><span>Total Bars</span>{metadata.get('total_bars', 0)}</div>
            </div>
        </header>

        <div class="audio-container">
            <audio id="audio-player" src="{metadata.get('audio_file', '')}" controls></audio>
            <div id="high-precision-time" style="font-family: 'Inter', monospace; font-size: 1.2rem; color: var(--accent-primary); margin-top: 0.5rem; font-weight: bold; text-align: center;">0.000s</div>
        </div>

        <div class="grid-container">
"""

    prev_chords = {
        'librosa': None,
        'essentia': None,
        'madmom': None,
        'final': None
    }

    for bar_num, bar_beats in sorted_bars:
        num_beats = len(bar_beats)
        if num_beats == 0:
            continue
            
        bar_start = bar_beats[0].get('start_time', 0.0)
        bar_end = bar_beats[-1].get('end_time', 0.0)
        
        html_content += f"""
            <div class="bar-card" data-start="{bar_start}" data-end="{bar_end}" onclick="seekAudio({bar_start})">
                <div class="bar-header">
                    <span>Bar {bar_num}</span>
                    <span class="bar-time">{bar_start:.1f}s</span>
                </div>
                <table class="bar-table">
                    <thead>
                        <tr>
                            <th>Model</th>"""
                            
        for i, beat in enumerate(bar_beats):
            b_start = beat.get('start_time', 0.0)
            b_end = beat.get('end_time', 0.0)
            html_content += f'<th class="beat-col" data-index="{i}" data-start="{b_start}" data-end="{b_end}">B{i+1}</th>'
            
        html_content += """
                        </tr>
                    </thead>
                    <tbody>"""

        models = [
            ('Librosa', 'librosa_chord', 'librosa'),
            ('Essentia', 'essentia_chord', 'essentia'),
            ('Madmom', 'madmom_chord', 'madmom'),
            ('Final', 'chord', 'final')
        ]

        for model_label, dict_key, state_key in models:
            row_class = "final-row" if state_key == "final" else ""
            html_content += f"""
                        <tr class="{row_class}">
                            <td class="model-name">{model_label}</td>"""
            
            for i, beat in enumerate(bar_beats):
                chord = beat.get(dict_key, 'N')
                
                # Check for repetition
                is_repeated = (chord == prev_chords[state_key])
                prev_chords[state_key] = chord
                
                classes = f"chord-val beat-cell-{i}"
                if chord == 'N':
                    classes += " none"
                if is_repeated:
                    classes += " repeated"
                    
                display_chord = chord if not is_repeated else ""
                
                html_content += f"""
                            <td class="{classes}">{display_chord}</td>"""
                            
            html_content += """
                        </tr>"""

        html_content += """
                    </tbody>
                </table>
            </div>"""

    html_content += """
        </div>
    </div>

    <script>
        const audio = document.getElementById('audio-player');
        const timeDisplay = document.getElementById('high-precision-time');
        const barCards = document.querySelectorAll('.bar-card');

        // Allow clicking a bar to seek the audio
        function seekAudio(time) {
            audio.currentTime = time;
            audio.play();
        }

        // Sync visualizer on time update
        audio.addEventListener('timeupdate', () => {
            const currentTime = audio.currentTime;
            timeDisplay.textContent = currentTime.toFixed(3) + 's';
            
            barCards.forEach(card => {
                const start = parseFloat(card.getAttribute('data-start'));
                const end = parseFloat(card.getAttribute('data-end'));
                
                // Highlight active bar
                if (currentTime >= start && currentTime < end) {
                    card.classList.add('active-bar');
                    
                    // Highlight active beat inside the bar
                    const beatCols = card.querySelectorAll('th.beat-col');
                    beatCols.forEach(col => {
                        const bStart = parseFloat(col.getAttribute('data-start'));
                        const bEnd = parseFloat(col.getAttribute('data-end'));
                        const index = col.getAttribute('data-index');
                        
                        // Select corresponding table cells
                        const cells = card.querySelectorAll(`.beat-cell-${index}`);
                        
                        if (currentTime >= bStart && currentTime < bEnd) {
                            col.classList.add('active-beat');
                            cells.forEach(c => c.classList.add('active-beat'));
                        } else {
                            col.classList.remove('active-beat');
                            cells.forEach(c => c.classList.remove('active-beat'));
                        }
                    });
                    
                    // Optional: scroll active bar into view if it's out of bounds
                    // const rect = card.getBoundingClientRect();
                    // if (rect.top < 0 || rect.bottom > window.innerHeight) {
                    //     card.scrollIntoView({ behavior: 'smooth', block: 'center' });
                    // }
                    
                } else {
                    card.classList.remove('active-bar');
                    // Remove active beats if bar is not active
                    const beatCols = card.querySelectorAll('th.beat-col');
                    beatCols.forEach(col => {
                        const index = col.getAttribute('data-index');
                        const cells = card.querySelectorAll(`.beat-cell-${index}`);
                        col.classList.remove('active-beat');
                        cells.forEach(c => c.classList.remove('active-beat'));
                    });
                }
            });
        });
    </script>
</body>
</html>
"""

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html_content)
