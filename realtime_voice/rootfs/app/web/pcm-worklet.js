class PcmCapture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.buffer = [];
  }

  process(inputs) {
    const samples = inputs[0]?.[0];
    if (!samples) return true;
    const ratio = sampleRate / 24000;
    const length = Math.floor(samples.length / ratio);
    const pcm = new Int16Array(length);
    for (let index = 0; index < length; index += 1) {
      const sample = Math.max(-1, Math.min(1, samples[Math.floor(index * ratio)]));
      pcm[index] = sample < 0 ? sample * 32768 : sample * 32767;
    }
    this.port.postMessage(pcm.buffer, [pcm.buffer]);
    return true;
  }
}

registerProcessor("pcm-capture", PcmCapture);
