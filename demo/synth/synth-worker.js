/* Renders a MIDI file to a 16-bit stereo WAV with FluidSynth (WebAssembly),
 * matching the backend's `fluidsynth -r 44100 -g 1.0` render with TimGM6mb.sf2. */
self.importScripts("libfluidsynth-2.4.6.js", "js-synthesizer.min.js");

const SAMPLE_RATE = 44100;
const BLOCK = 4096;
const TAIL_SECONDS = 1.5;
const MAX_SECONDS = 15 * 60;

let soundfontPromise = null;

function loadSoundfont(url) {
  if (!soundfontPromise) {
    soundfontPromise = fetch(url).then((response) => {
      if (!response.ok) throw new Error(`Soundfont ${response.status}`);
      return response.arrayBuffer();
    });
  }
  return soundfontPromise;
}

function encodeWav(left, right, frames) {
  const bytesPerFrame = 4;
  const buffer = new ArrayBuffer(44 + frames * bytesPerFrame);
  const view = new DataView(buffer);
  const writeString = (offset, text) => {
    for (let i = 0; i < text.length; i += 1) view.setUint8(offset + i, text.charCodeAt(i));
  };
  writeString(0, "RIFF");
  view.setUint32(4, 36 + frames * bytesPerFrame, true);
  writeString(8, "WAVE");
  writeString(12, "fmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 2, true);
  view.setUint32(24, SAMPLE_RATE, true);
  view.setUint32(28, SAMPLE_RATE * bytesPerFrame, true);
  view.setUint16(32, bytesPerFrame, true);
  view.setUint16(34, 16, true);
  writeString(36, "data");
  view.setUint32(40, frames * bytesPerFrame, true);

  let offset = 44;
  let frame = 0;
  for (let c = 0; c < left.length && frame < frames; c += 1) {
    const l = left[c];
    const r = right[c];
    for (let i = 0; i < l.length && frame < frames; i += 1, frame += 1) {
      const ls = Math.max(-1, Math.min(1, l[i]));
      const rs = Math.max(-1, Math.min(1, r[i]));
      view.setInt16(offset, ls < 0 ? ls * 0x8000 : ls * 0x7fff, true);
      view.setInt16(offset + 2, rs < 0 ? rs * 0x8000 : rs * 0x7fff, true);
      offset += 4;
    }
  }
  return buffer;
}

let loggingDisabled = false;

async function render(midiUrl, soundfontUrl) {
  await JSSynth.waitForReady();
  if (!loggingDisabled && JSSynth.disableLogging) {
    JSSynth.disableLogging(2);
    loggingDisabled = true;
  }
  const [soundfont, midi] = await Promise.all([
    loadSoundfont(soundfontUrl),
    fetch(midiUrl).then((response) => {
      if (!response.ok) throw new Error(`MIDI ${response.status}`);
      return response.arrayBuffer();
    }),
  ]);

  const synth = new JSSynth.Synthesizer();
  synth.init(SAMPLE_RATE);
  synth.setGain(1.0);
  try {
    await synth.loadSFont(soundfont.slice(0));
    await synth.addSMFDataToPlayer(midi);
    await synth.playPlayer();

    const left = [];
    const right = [];
    let tailBlocks = Math.ceil((TAIL_SECONDS * SAMPLE_RATE) / BLOCK);
    const maxBlocks = Math.ceil((MAX_SECONDS * SAMPLE_RATE) / BLOCK);
    while (left.length < maxBlocks) {
      const playing = synth.isPlayerPlaying();
      if (!playing && tailBlocks-- <= 0) break;
      const l = new Float32Array(BLOCK);
      const r = new Float32Array(BLOCK);
      synth.render([l, r]);
      left.push(l);
      right.push(r);
    }
    return encodeWav(left, right, left.length * BLOCK);
  } finally {
    synth.close();
  }
}

self.onmessage = async (event) => {
  const { id, midiUrl, soundfontUrl } = event.data;
  try {
    const wav = await render(midiUrl, soundfontUrl);
    self.postMessage({ id, wav }, [wav]);
  } catch (error) {
    self.postMessage({ id, error: String(error && error.message ? error.message : error) });
  }
};
