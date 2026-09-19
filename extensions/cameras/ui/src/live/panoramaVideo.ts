/** Renderer consumes optical geometry, never actuator units or viewport transforms. */
export type VideoGeometry = {
    content_rect?: {
        x: number;
        y: number;
        width: number;
        height: number;
    };
    lens: {
        width: number;
        height: number;
        fx: number;
        fy: number;
        cx: number;
        cy: number;
        distortion?: number[];
    };
    panorama_to_camera: number[][];
};

/**
 * Rejects a registration only when change is spread through the image. A
 * person, foliage, compression noise, or a small exposure adjustment must not
 * look like a PTZ move. This is a veto for an estimator result, never evidence
 * that a pose is correct by itself.
 */
export function frameStillSharesRegisteredView(
    registered: Uint8ClampedArray,
    current: Uint8ClampedArray,
    width: number,
    height: number,
): boolean {
    if (width < 1 || height < 1 || registered.length !== current.length || registered.length !== width * height * 4)
        return false;
    const columns = Math.min(10, width), rows = Math.min(6, height);
    const changedByTile = new Uint32Array(columns * rows);
    const pixelsByTile = new Uint32Array(columns * rows);
    let changedPixels = 0;
    for (let y = 0; y < height; y++) {
        const tileY = Math.min(rows - 1, Math.floor(y * rows / height));
        for (let x = 0; x < width; x++) {
            const pixel = (y * width + x) * 4;
            const tile = tileY * columns + Math.min(columns - 1, Math.floor(x * columns / width));
            pixelsByTile[tile]++;
            const colorDifference = (
                Math.abs(registered[pixel] - current[pixel])
                + Math.abs(registered[pixel + 1] - current[pixel + 1])
                + Math.abs(registered[pixel + 2] - current[pixel + 2])
            ) / 3;
            if (colorDifference > 32) {
                changedPixels++;
                changedByTile[tile]++;
            }
        }
    }
    let changedTiles = 0;
    for (let tile = 0; tile < changedByTile.length; tile++)
        if (changedByTile[tile] / pixelsByTile[tile] > .55)
            changedTiles++;
    return changedPixels / (width * height) < .58 && changedTiles / changedByTile.length < .5;
}
export function panoramaVideoPixel(u: number, v: number, geometry: VideoGeometry): [
    number,
    number
] | null {
    if (![u, v].every(Number.isFinite) || u < 0 || u > 1 || v < 0 || v > 1)
        return null;
    const pan = (u * 2 - 1) * Math.PI, elevation = (0.5 - v) * Math.PI;
    const ray = [Math.cos(elevation) * Math.cos(pan), Math.cos(elevation) * Math.sin(pan), Math.sin(elevation)];
    const local = geometry.panorama_to_camera.map(row => row.reduce((s, x, i) => s + x * ray[i], 0));
    if (local[2] <= 0.0001)
        return null;
    const x = local[0] / local[2], y = local[1] / local[2], r = x * x + y * y;
    const [k1 = 0, k2 = 0, p1 = 0, p2 = 0, k3 = 0, k4 = 0, k5 = 0, k6 = 0] = geometry.lens.distortion ?? [];
    const denominator = 1 + k4 * r + k5 * r * r + k6 * r * r * r;
    if (Math.abs(denominator) < 1e-8)
        return null;
    const radial = (1 + k1 * r + k2 * r * r + k3 * r * r * r) / denominator;
    const px = geometry.lens.fx * (x * radial + 2 * p1 * x * y + p2 * (r + 2 * x * x)) + geometry.lens.cx;
    const py = geometry.lens.fy * (y * radial + p1 * (r + 2 * y * y) + 2 * p2 * x * y) + geometry.lens.cy;
    return Number.isFinite(px + py) && px >= 0 && py >= 0 && px <= geometry.lens.width - 1 && py <= geometry.lens.height - 1 ? [px, py] : null;
}
const fragment = `precision highp float;
varying vec2 panorama;
uniform float horizontalCopies;
uniform sampler2D videoImage;
uniform sampler2D coverage;
uniform mat3 panoramaToCamera;
uniform vec4 intrinsics;
uniform vec2 imageSize;
uniform vec4 videoContentRect;
uniform vec4 distortionA;
uniform vec4 distortionB;
void main() {
  vec2 coordinate=vec2(fract(panorama.x*horizontalCopies),panorama.y);
  if (texture2D(coverage, coordinate).r < 0.01) discard;
  float pan = (coordinate.x*2.0-1.0)*3.141592653589793;
  float elevation = (0.5-coordinate.y)*3.141592653589793;
  vec3 ray = panoramaToCamera * vec3(cos(elevation)*cos(pan),cos(elevation)*sin(pan),sin(elevation));
  if (ray.z <= 0.0001) discard;
  vec2 p=ray.xy/ray.z;
  float r=dot(p,p);
  float denominator=1.0+distortionB.y*r+distortionB.z*r*r+distortionB.w*r*r*r;
  if (abs(denominator)<0.00000001) discard;
  float radial=(1.0+distortionA.x*r+distortionA.y*r*r+distortionB.x*r*r*r)/denominator;
  vec2 q=p*radial+vec2(2.0*distortionA.z*p.x*p.y+distortionA.w*(r+2.0*p.x*p.x),distortionA.z*(r+2.0*p.y*p.y)+2.0*distortionA.w*p.x*p.y);
  vec2 pixel=q*intrinsics.xy+intrinsics.zw;
  if (any(lessThan(pixel,vec2(0.0))) || any(greaterThan(pixel,imageSize-1.0))) discard;
  gl_FragColor=vec4(texture2D(videoImage,videoContentRect.xy+(pixel+0.5)/imageSize*videoContentRect.zw).rgb,1.0);
}`;
export function createPanoramaVideoRenderer(canvas: HTMLCanvasElement, mask: TexImageSource, horizontalCopies = 1) {
    const gl = canvas.getContext('webgl', { alpha: true, premultipliedAlpha: false, preserveDrawingBuffer: false });
    if (!gl)
        throw new Error('Projeção de vídeo indisponível neste navegador');
    const shaders: WebGLShader[] = [];
    function shader(type: number, source: string) {
        const value = gl!.createShader(type)!;
        shaders.push(value);
        gl!.shaderSource(value, source);
        gl!.compileShader(value);
        if (!gl!.getShaderParameter(value, gl!.COMPILE_STATUS))
            throw new Error(gl!.getShaderInfoLog(value) ?? 'Shader inválido');
        return value;
    }
    const program = gl.createProgram()!;
    gl.attachShader(program, shader(gl.VERTEX_SHADER, 'attribute vec2 position; varying vec2 panorama; void main(){panorama=vec2((position.x+1.0)*0.5,(1.0-position.y)*0.5);gl_Position=vec4(position,0.0,1.0);}'));
    gl.attachShader(program, shader(gl.FRAGMENT_SHADER, fragment));
    gl.linkProgram(program);
    if (!gl.getProgramParameter(program, gl.LINK_STATUS))
        throw new Error('Projeção de vídeo indisponível');
    gl.useProgram(program);
    const buffer = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, -1, 1, 1, -1, 1, 1]), gl.STATIC_DRAW);
    const position = gl.getAttribLocation(program, 'position');
    gl.enableVertexAttribArray(position);
    gl.vertexAttribPointer(position, 2, gl.FLOAT, false, 0, 0);
    const textures = [0, 1].map(index => { const texture = gl.createTexture(); gl.activeTexture(gl.TEXTURE0 + index); gl.bindTexture(gl.TEXTURE_2D, texture); gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, index === 1 ? gl.NEAREST : gl.LINEAR); gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, index === 1 ? gl.NEAREST : gl.LINEAR); gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE); gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE); return texture; });
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, mask);
    const uniform = (name: string) => gl.getUniformLocation(program, name);
    gl.uniform1f(uniform('horizontalCopies'), horizontalCopies);
    gl.uniform1i(uniform('videoImage'), 0);
    gl.uniform1i(uniform('coverage'), 1);
    return {
        clear() { gl.clearColor(0, 0, 0, 0); gl.clear(gl.COLOR_BUFFER_BIT); },
        draw(image: TexImageSource, geometry: VideoGeometry) {
            gl.viewport(0, 0, canvas.width, canvas.height);
            gl.clear(gl.COLOR_BUFFER_BIT);
            gl.useProgram(program);
            const lens = geometry.lens, d = [...(lens.distortion ?? []), 0, 0, 0, 0, 0, 0, 0, 0];
            gl.uniformMatrix3fv(uniform('panoramaToCamera'), false, new Float32Array([0, 1, 2].flatMap(column => geometry.panorama_to_camera.map(row => row[column]))));
            const rect = geometry.content_rect ?? { x: 0, y: 0, width: 1, height: 1 };
            gl.uniform4f(uniform('videoContentRect'), rect.x, rect.y, rect.width, rect.height);
            gl.uniform4f(uniform('intrinsics'), lens.fx, lens.fy, lens.cx, lens.cy);
            gl.uniform2f(uniform('imageSize'), lens.width, lens.height);
            gl.uniform4fv(uniform('distortionA'), d.slice(0, 4));
            gl.uniform4fv(uniform('distortionB'), d.slice(4, 8));
            gl.activeTexture(gl.TEXTURE0);
            gl.bindTexture(gl.TEXTURE_2D, textures[0]);
            gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, image);
            gl.drawArrays(gl.TRIANGLES, 0, 6);
        },
        dispose() { textures.forEach(t => gl.deleteTexture(t)); gl.deleteBuffer(buffer); gl.deleteProgram(program); shaders.forEach(s => gl.deleteShader(s)); },
    };
}
