/** WebGL2 renderer. Planes are uploaded as float textures holding the *data values*; the
 *  contrast window, gamma, inversion and colour table are applied in the fragment shader, so
 *  changing any of them is a redraw, never a request. */

import type { Region } from "../api/types";
import type { Lut } from "./luts";
import { imageToScreen, type Size, type View } from "./view";

const VERTEX = `#version 300 es
in vec2 aPos;
uniform vec4 uRect; // x0, y0, x1, y1 in clip space
out vec2 vUv;
void main() {
  vUv = aPos;
  gl_Position = vec4(mix(uRect.xy, uRect.zw, aPos), 0.0, 1.0);
}`;

const FRAGMENT = `#version 300 es
precision highp float;
precision highp sampler2D;
in vec2 vUv;
out vec4 outColor;
uniform sampler2D uTex0;
uniform sampler2D uTex1;
uniform sampler2D uTex2;
uniform sampler2D uLut;
uniform vec2 uTexSize;
uniform float uMin;
uniform float uMax;
uniform float uGamma;
uniform bool uInvert;
uniform bool uRgb;
uniform vec3 uNanColor;

// One texel when magnifying (pixels stay sharp squares); the mean of the four nearest when a
// texel is smaller than a screen pixel, so that reducing never just picks one value out of N.
float fetch(sampler2D tex) {
  vec2 p = vUv * uTexSize;
  ivec2 last = ivec2(uTexSize) - 1;
  vec2 footprint = fwidth(p);
  if (max(footprint.x, footprint.y) <= 1.0) {
    return texelFetch(tex, clamp(ivec2(floor(p)), ivec2(0), last), 0).r;
  }
  vec2 q = p - 0.5;
  ivec2 i = ivec2(floor(q));
  vec2 f = fract(q);
  float a = texelFetch(tex, clamp(i, ivec2(0), last), 0).r;
  float b = texelFetch(tex, clamp(i + ivec2(1, 0), ivec2(0), last), 0).r;
  float c = texelFetch(tex, clamp(i + ivec2(0, 1), ivec2(0), last), 0).r;
  float d = texelFetch(tex, clamp(i + ivec2(1, 1), ivec2(0), last), 0).r;
  return mix(mix(a, b, f.x), mix(c, d, f.x), f.y);
}

float tone(float v) {
  float t = clamp((v - uMin) / (uMax - uMin), 0.0, 1.0);
  t = pow(t, 1.0 / uGamma);
  return uInvert ? 1.0 - t : t;
}

void main() {
  float v0 = fetch(uTex0);
  if (uRgb) {
    float v1 = fetch(uTex1);
    float v2 = fetch(uTex2);
    if (!(v0 == v0) || !(v1 == v1) || !(v2 == v2)) { outColor = vec4(uNanColor, 1.0); return; }
    outColor = vec4(tone(v0), tone(v1), tone(v2), 1.0);
    return;
  }
  if (!(v0 == v0)) { outColor = vec4(uNanColor, 1.0); return; } // NaN: not a grey level
  float t = tone(v0);
  outColor = vec4(texture(uLut, vec2((t * 255.0 + 0.5) / 256.0, 0.5)).rgb, 1.0);
}`;

export interface GpuLayer {
  textures: WebGLTexture[]; // one per channel shown (1, or 3 for RGB)
  width: number; // texels
  height: number;
  region: Region; // full-resolution image pixels covered
  bin: number;
}

export interface DrawParams {
  view: View;
  viewport: Size; // CSS pixels
  layers: GpuLayer[]; // drawn in order: overview first, detail on top
  min: number;
  max: number;
  gamma: number;
  invert: boolean;
  rgb: boolean;
}

export class WebGLUnavailable extends Error {}

export class Renderer {
  readonly gl: WebGL2RenderingContext;
  readonly maxTextureSize: number;
  private readonly program: WebGLProgram;
  private readonly uniforms: Record<string, WebGLUniformLocation | null> = {};
  private readonly lut: WebGLTexture;
  private readonly vao: WebGLVertexArrayObject;

  constructor(private readonly canvas: HTMLCanvasElement) {
    const gl = canvas.getContext("webgl2", { antialias: false, alpha: false, preserveDrawingBuffer: true });
    if (!gl) throw new WebGLUnavailable("WebGL2 n'est pas disponible dans ce navigateur");
    this.gl = gl;
    this.maxTextureSize = gl.getParameter(gl.MAX_TEXTURE_SIZE) as number;
    this.program = this.link(VERTEX, FRAGMENT);
    for (const name of ["uRect", "uTex0", "uTex1", "uTex2", "uLut", "uTexSize", "uMin", "uMax", "uGamma", "uInvert", "uRgb", "uNanColor"]) {
      this.uniforms[name] = gl.getUniformLocation(this.program, name);
    }
    const vao = gl.createVertexArray();
    const buffer = gl.createBuffer();
    if (!vao || !buffer) throw new WebGLUnavailable("WebGL2: allocation impossible");
    this.vao = vao;
    gl.bindVertexArray(vao);
    gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([0, 0, 1, 0, 0, 1, 1, 1]), gl.STATIC_DRAW);
    const loc = gl.getAttribLocation(this.program, "aPos");
    gl.enableVertexAttribArray(loc);
    gl.vertexAttribPointer(loc, 2, gl.FLOAT, false, 0, 0);
    this.lut = this.texture();
    gl.pixelStorei(gl.UNPACK_ALIGNMENT, 1);
  }

  private link(vertex: string, fragment: string): WebGLProgram {
    const gl = this.gl;
    const compile = (type: number, source: string): WebGLShader => {
      const shader = gl.createShader(type);
      if (!shader) throw new WebGLUnavailable("WebGL2: shader impossible");
      gl.shaderSource(shader, source);
      gl.compileShader(shader);
      if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
        throw new WebGLUnavailable(`shader: ${gl.getShaderInfoLog(shader) ?? "erreur"}`);
      }
      return shader;
    };
    const program = gl.createProgram();
    if (!program) throw new WebGLUnavailable("WebGL2: programme impossible");
    gl.attachShader(program, compile(gl.VERTEX_SHADER, vertex));
    gl.attachShader(program, compile(gl.FRAGMENT_SHADER, fragment));
    gl.linkProgram(program);
    if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
      throw new WebGLUnavailable(`programme: ${gl.getProgramInfoLog(program) ?? "erreur"}`);
    }
    return program;
  }

  private texture(): WebGLTexture {
    const gl = this.gl;
    const tex = gl.createTexture();
    if (!tex) throw new WebGLUnavailable("WebGL2: texture impossible");
    gl.bindTexture(gl.TEXTURE_2D, tex);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    return tex;
  }

  setLut(lut: Lut): void {
    const gl = this.gl;
    gl.activeTexture(gl.TEXTURE3);
    gl.bindTexture(gl.TEXTURE_2D, this.lut);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA8, 256, 1, 0, gl.RGBA, gl.UNSIGNED_BYTE, lut);
  }

  /** Upload one plane per channel (same size) as R32F textures. */
  createLayer(channels: Float32Array[], width: number, height: number, region: Region, bin: number): GpuLayer {
    if (width > this.maxTextureSize || height > this.maxTextureSize) {
      throw new WebGLUnavailable(`image trop grande pour cette carte graphique (${width} × ${height} texels)`);
    }
    const gl = this.gl;
    const textures = channels.map((data) => {
      const tex = this.texture();
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.R32F, width, height, 0, gl.RED, gl.FLOAT, data);
      return tex;
    });
    return { textures, width, height, region, bin };
  }

  deleteLayer(layer: GpuLayer | null | undefined): void {
    for (const tex of layer?.textures ?? []) this.gl.deleteTexture(tex);
  }

  /** Match the drawing buffer to the CSS size and the pixel density. */
  resize(viewport: Size, devicePixelRatio: number): void {
    const w = Math.max(1, Math.round(viewport.width * devicePixelRatio));
    const h = Math.max(1, Math.round(viewport.height * devicePixelRatio));
    if (this.canvas.width !== w || this.canvas.height !== h) {
      this.canvas.width = w;
      this.canvas.height = h;
    }
  }

  draw(p: DrawParams): void {
    const gl = this.gl;
    const u = this.uniforms;
    gl.viewport(0, 0, this.canvas.width, this.canvas.height);
    gl.clearColor(0.035, 0.045, 0.06, 1);
    gl.clear(gl.COLOR_BUFFER_BIT);
    gl.useProgram(this.program);
    gl.bindVertexArray(this.vao);
    gl.uniform1f(u.uMin!, p.min);
    gl.uniform1f(u.uMax!, p.max > p.min ? p.max : p.min + 1);
    gl.uniform1f(u.uGamma!, p.gamma > 0 ? p.gamma : 1);
    gl.uniform1i(u.uInvert!, p.invert ? 1 : 0);
    gl.uniform1i(u.uRgb!, p.rgb ? 1 : 0);
    gl.uniform3f(u.uNanColor!, 0.45, 0.1, 0.35);
    gl.activeTexture(gl.TEXTURE3);
    gl.bindTexture(gl.TEXTURE_2D, this.lut);
    gl.uniform1i(u.uLut!, 3);
    for (const layer of p.layers) {
      const [x, y] = layer.region;
      // A texel covers bin x bin image pixels: the quad spans whole texels, so the grids of
      // the overview and of the detail coincide (the last texel may overhang a ragged edge).
      const [sx0, sy0] = imageToScreen(p.view, p.viewport, x, y);
      const [sx1, sy1] = imageToScreen(p.view, p.viewport, x + layer.width * layer.bin, y + layer.height * layer.bin);
      const cx = (s: number) => (s / p.viewport.width) * 2 - 1;
      const cy = (s: number) => 1 - (s / p.viewport.height) * 2;
      gl.uniform4f(u.uRect!, cx(sx0), cy(sy0), cx(sx1), cy(sy1));
      gl.uniform2f(u.uTexSize!, layer.width, layer.height);
      layer.textures.slice(0, 3).forEach((tex, i) => {
        gl.activeTexture(gl.TEXTURE0 + i);
        gl.bindTexture(gl.TEXTURE_2D, tex);
        gl.uniform1i(u[`uTex${i}`]!, i);
      });
      gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
    }
  }

  /** Colour of the drawing buffer at a CSS position (tests and diagnostics). */
  readPixel(x: number, y: number, devicePixelRatio: number): [number, number, number, number] {
    const gl = this.gl;
    const out = new Uint8Array(4);
    const px = Math.floor(x * devicePixelRatio);
    const py = this.canvas.height - 1 - Math.floor(y * devicePixelRatio);
    gl.readPixels(px, py, 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, out);
    return [out[0] ?? 0, out[1] ?? 0, out[2] ?? 0, out[3] ?? 0];
  }

  dispose(): void {
    this.gl.getExtension("WEBGL_lose_context")?.loseContext();
  }
}
