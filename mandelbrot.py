#!/usr/bin/env python3

"""Compute and draw/explore/animate the Mandelbrot set.

Fast computation of the Mandelbrot set using Numba on CPU or GPU. The set is
smoothly colored with custom colortables. 

  mand = Mandelbrot()
  mand.explore()
"""

import math
import numpy as np
import matplotlib.pyplot as plt
from numba import jit
from matplotlib.widgets import Slider, Button
from numba import cuda
from PIL import Image
import imageio


def sin_colortable(rgb_thetas=[.85, .0, .15], ncol=2**12):
    """ Sinusoidal color table
    
    Cyclic and smooth color table made with a sinus function for each color
    channel
    
    Args:
        rgb_thetas: [float, float, float]
            phase for each color channel
        ncol: int 
            number of color in the output table

    Returns:
        ndarray(dtype=float, ndim=2): color table
    """
    def colormap(x, rgb_thetas):
        # x in [0,1]
        # Compute the frequency and phase of each channel
        y = x*2*math.pi
        y = np.column_stack((y + rgb_thetas[0] * 2 * math.pi,
                             y + rgb_thetas[1] * 2 * math.pi,
                             y + rgb_thetas[2] * 2 * math.pi))
        # Set amplitude to [0,255]
        val = 0.5 + 0.5*np.sin(y)
        return val

    return colormap(np.linspace(0, 1, ncol), rgb_thetas)

@jit
def smooth_iter(c, maxiter, stripe_s, stripe_sig):
    """ Smooth number of iteration in the Mandelbrot set for given c
    
    Args:
        c: complex
            point of the complex plane
        maxiter: int 
            maximal number of iterations

    Returns:
        float: smooth iteration count at escape, 0 if maxiter is reached
    """
    # Escape radius squared: 2**2 is enough, but using a higher radius yields
    # better estimate of the smooth iteration count and the stripes
    esc_radius_2 = 10**10
    z = complex(0, 0)
    
    # Stripe average coloring if parameters are given
    stripe = (stripe_s > 0) and (stripe_sig > 0)
    # z derivative
    dz = 1+0j
    stripe_a =  0
            
    # Mandelbrot iteration
    for n in range(maxiter):
        dz = dz*2*z + 1
        z = z*z + c
        if stripe:
            # Stripe Average Coloring
            # See: Jussi Harkonen On Smooth Fractal Coloring Techniques
            # cos instead of sin for symmetry
            # np.angle inavailable in CUDA
            # np.angle(z) = math.atan2(z.imag, z.real)
            stripe_t = (math.sin(stripe_s*math.atan2(z.imag, z.real)) + 1) / 2
        
        # If escape: save (smooth) iteration count
        # Equivalent to abs(z) > esc_radius
        if z.real*z.real + z.imag*z.imag > esc_radius_2:
            
            modz = abs(z)
            
            # Smooth iteration count: equals n when abs(z) = esc_radius
            log_ratio = 2*math.log(modz)/math.log(esc_radius_2)
            smooth_i = 1 - math.log(log_ratio)/math.log(2)

            if stripe:
                # Stripe average coloring
                # Smoothing + linear interpolation
                stripe_a = (stripe_a * (1 + smooth_i * (stripe_sig-1)) +
                            stripe_t * smooth_i * (1 - stripe_sig))
                # Same as 2 following lines:
                #a2 = a * stripe_sig + stripe_t * (1-stripe_sig)
                #a = a * (1 - smooth_i) + a2 * smooth_i            
                # Init correction, init weight is now: 
                # stripe_sig**n * (1 + smooth_i * (stripe_sig-1))
                # thus, a's weight is 1 - init_weight. We rescale
                stripe_a = stripe_a / (1 - stripe_sig**n *
                                       (1 + smooth_i * (stripe_sig-1))) 

            # Normal vector for lighting
            u = z/dz
            u = u/abs(u)
            normal = u # 3D vector (u.real, u.imag. 1)

            # Milton's distance estimator
            dem = modz * math.log(modz) / abs(dz) / 2

            # real niter: n+1
            # real smoothiter: n+smooth_i (1 > smooth_i > 0) 
            # so smoothiter <= niter, in particular: smoothiter <= maxiter 
            # stripe_a: between 0 et 1
            # dem: distance to set boundary
            return (n+smooth_i, stripe_a, dem, normal)
        
        if stripe:
            stripe_a = stripe_a * stripe_sig + stripe_t * (1-stripe_sig)
            
    # Otherwise: set iteration count to 0
    return (0,0,0,0)

@jit
def color_pixel(matxy, niter, stripe_a, dem, normal, colortable, ncycle,
                light):
    """Color inplace"""

    ncol = colortable.shape[0] - 1
    # Power post-transform
    niter = math.sqrt(niter)
    # Cycle through colortable
    col_i = round(niter % ncycle / ncycle * ncol)
    # dem: log transform and sigmoid on [0,1] => [0,1]
    dem = -math.log(dem)/12
    dem = 1/(1+math.exp(-10*((2*dem-1)/2)))
    
    def overlay(x, y, gamma):
        """x, y  and gamma floats in [0,1]. Returns float in [0,1]"""
        if (2*y) < 1:
            out = 2*x*y
        else:
            out = 1 - 2 * (1 - x) * (1 - y)
        return out * gamma + x * (1-gamma)
    def softlight(x,y):
        """x and y floats in [0,1]. Returns float in [0,1]"""
        return (1 - 2*x)*y**2 + 2*x*y
    
    # Light vector: angle direction, height, intensity
    # Lambert normal shading
    # light vector: [math.cos(light[0]), math.sin(light[0])]
    # Light brightness value: dot product between light and normal
    lbright = (normal.real*math.cos(light[0]) +
               normal.imag*math.sin(light[0]) + light[1])
    lbright = lbright/(1+light[1])
    # clip to [0,1]
    lbright = min(1, max(0, lbright))
    
    # pixel brightness        
    br = (colortable[col_i,0] + colortable[col_i,1] + colortable[col_i,2])/3
    
    for i in range(3):
        matxy[i] = colortable[col_i,i]
        # Stripe average coloring: using 3 blend modes
        if stripe_a > 0:
            # Softlight: good mode but lowers the saturation
            sl = softlight(matxy[i], stripe_a)
            # Brightness correction: increases too much the saturation,
            # and has non-continuity when base brightness is close to 0
            brc = matxy[i] * stripe_a / br
            # Opacity for low brightness (to hide non-continuity)
            brc = brc * math.sqrt(br) + (1-math.sqrt(br)) * matxy[i]
            # Overlay: good, but information loss when base is close to 0
            # or 1
            ol = overlay(matxy[i], stripe_a, 1)
            # Average of three modes
            matxy[i] = (sl + brc + ol) / 3
            # Opacity with distance estimator: hide the stripes for the
            # points closer to the set
            matxy[i] = matxy[i] * (1-dem) + dem * colortable[col_i,i]
        # Lambert shading with overlay mode
        matxy[i] = overlay(matxy[i], lbright, light[2])
        matxy[i] = min(1, matxy[i])

@jit
def compute_set(creal, cim, maxiter, colortable, ncycle, stripe_s, stripe_sig,
                diag, light):
    """ Compute and color the Mandelbrot set (CPU version)
    
    Args:
        creal: ndarray(dtype=float, ndim=1)
            vector of real coordinates
        cim: ndarray(dtype=float, ndim=1)
            vector of imaginary coordinates
        maxiter: int 
            maximal number of iterations
        colortable: ndarray(dtype=uint8, ndim=2)
            cyclic RGB colortable 
        ncycle: float
            number of iteration before cycling the colortable

    Returns:
        ndarray(dtype=uint8, ndim=3): image of the Mandelbrot set
    """
    xpixels = len(creal)
    ypixels = len(cim)

    # Output initialization
    mat = np.zeros((ypixels, xpixels, 3))

    # Looping through pixels
    for x in range(xpixels):
        for y in range(ypixels):
            # Initialization of c
            c = complex(creal[x], cim[y])
            # Get smooth iteration count
            niter, stripe_a, dem, normal = smooth_iter(c, maxiter, stripe_s,
                                                      stripe_sig)
            # If escaped: color the set
            if niter > 0:
                # dem normalization by diag
                color_pixel(mat[y,x,], niter, stripe_a, dem/diag, normal, colortable,
                            ncycle, light)
    return mat

@cuda.jit
def compute_set_gpu(mat, xmin, xmax, ymin, ymax, maxiter, colortable, ncycle,
                    stripe_s, stripe_sig, diag, light):
    """ Compute and color the Mandelbrot set (GPU version)
    
    Uses a 1D-grid with blocks of 32 threads.
    
    Args:
        mat: ndarray(dtype=uint8, ndim=3)
            shared data to write the output image of the set
        xmin, xmax, ymin, ymax: float
            coordinates of the set
        maxiter: int 
            maximal number of iterations
        colortable: ndarray(dtype=uint8, ndim=2)
            cyclic RGB colortable 
        ncycle: float
            number of iteration before cycling the colortable

    Returns:
        mat: ndarray(dtype=uint8, ndim=3)
            shared data to write the output image of the set
    """
    # Retrieve x and y from CUDA grid coordinates
    index = cuda.grid(1)
    x, y = index % mat.shape[1], index // mat.shape[1]
    #ncol = colortable.shape[0] - 1
    
    # Check if x and y are not out of mat bounds
    if (y < mat.shape[0]) and (x < mat.shape[1]):
        # Mapping pixel to C
        creal = xmin + x / (mat.shape[1] - 1) * (xmax - xmin)
        cim = ymin + y / (mat.shape[0] - 1) * (ymax - ymin)
        # Initialization of c
        c = complex(creal, cim)
        # Get smooth iteration count
        niter, stripe_a, dem, normal = smooth_iter(c, maxiter, stripe_s, stripe_sig)
        # If escaped: color the set
        if niter > 0:
            color_pixel(mat[y,x,], niter, stripe_a, dem/diag, normal,
                        colortable, ncycle, light)

class Mandelbrot():
    """Mandelbrot set object"""
    def __init__(self, xpixels=1280, maxiter=500,
                 coord=(-2.6, 1.845, -1.25, 1.25), gpu=False, ncycle=32,
                 rgb_thetas=[.15, .9, 0], oversampling=1, stripe_s=5,
                 stripe_sig=.9, light = [math.pi/2, 1., .3]):
        """Mandelbrot set object
    
        Args:
            xpixels: int
                image width (in pixels)
            maxiter: int
                maximal number of iterations
            coord: (float, float, float, float)
                coordinates of the frame in the complex space. Default to the
                main view of the Set, with a 16:9 ratio.
            gpu: boolean
                use CUDA on GPU to compute the set
            ncycle: float
                number of iteration before cycling the colortable
            colortable: ndarray(dtype=uint8, ndim=2)
                color table used to color the set (preferably cyclic)
            oversampling: int
                for each pixel, a [n, n] grid is computed where n is the
                oversampling_size. Then, the average color of the n*n pixels
                is taken. Set to 1 for no oversampling.
        """
        
        self.xpixels = xpixels
        self.maxiter = maxiter
        self.coord = coord
        self.gpu = gpu
        self.ncycle = ncycle
        self.os = oversampling
        self.rgb_thetas = rgb_thetas
        self.stripe_s = stripe_s
        self.stripe_sig = stripe_sig
        self.light = np.array(light)
        # Compute ypixels so the image is not stretched (1:1 ratio)
        self.ypixels = round(self.xpixels / (self.coord[1]-self.coord[0]) *
                             (self.coord[3]-self.coord[2]))
        # Initialization of colortable
        self.colortable = sin_colortable(self.rgb_thetas)
        # Compute the set
        self.update_set()

    def update_set(self):
        """Updates the set
    
        Compute and color the Mandelbrot set, using CPU or GPU
        """
        # Apply ower post-transform to ncycle
        ncycle = math.sqrt(self.ncycle)
        diag = math.sqrt((self.coord[1]-self.coord[0])**2 +
                  (self.coord[3]-self.coord[2])**2)
        # Oversampling: rescaling by os
        xp = self.xpixels*self.os
        yp = self.ypixels*self.os
        
        if(self.gpu):
            # Pixel mapping is done in compute_self_gpu
            self.set = np.zeros((yp, xp, 3))
            # Compute set with GPU: 
            # 1D grid, with n blocks of 32 threads 
            npixels = xp * yp
            nthread = 32
            nblock = math.ceil(npixels / nthread)
            compute_set_gpu[nblock,
                            nthread](self.set, *self.coord, self.maxiter,
                                    self.colortable, ncycle, self.stripe_s,
                                    self.stripe_sig, diag, self.light)
        else:
            # Mapping pixels to C
            creal = np.linspace(self.coord[0], self.coord[1], xp)
            cim = np.linspace(self.coord[2], self.coord[3], yp)
            # Compute set with CPU
            self.set = compute_set(creal, cim, self.maxiter,
                                   self.colortable, ncycle, self.stripe_s,
                                   self.stripe_sig, diag,
                                   self.light)
        self.set = (255*self.set).astype(np.uint8)
        # Oversampling: reshaping to (ypixels, xpixels, 3)
        if self.os > 1:
            self.set = (self.set
                        .reshape((self.ypixels, self.os,
                                  self.xpixels, self.os, 3))
                        .mean(3).mean(1).astype(np.uint8))
    
    def draw(self, filename = None):
        # Reverse x-axis (equivalent to matplotlib's origin='lower')
        img = Image.fromarray(self.set[::-1,:,:], 'RGB')
        if filename is not None:
            img.save(filename) # fast (save in jpg) (compare reading as well)
        else:
            img.show() # slow
            
    def draw_mpl(self, filename=None, dpi=72):
        """Draw or save, using Matplotlib"""
        plt.subplots(figsize=(self.xpixels/dpi, self.ypixels/dpi))
        plt.imshow(self.set, extent=self.coord, origin='lower')
        # Remove axis and margins
        plt.subplots_adjust(left=0, right=1, bottom=0, top=1)
        plt.axis('off')
        # Write figure to file
        if filename is not None:
            plt.savefig(filename, dpi=dpi)
        else:
            plt.show()
        
    def zoom_at(self, x, y, s):
        """Zoom at (x,y): center at (x,y) and scale by s"""
        xrange = (self.coord[1] - self.coord[0])/2
        yrange = (self.coord[3] - self.coord[2])/2
        self.coord = [x - xrange * s,
                      x + xrange * s,
                      y - yrange * s,
                      y + yrange * s]
        
    def szoom_at(self, x, y, s):
        """Soft zoom (continuous) at (x,y): partial centering"""
        xrange = (self.coord[1] - self.coord[0])/2
        yrange = (self.coord[3] - self.coord[2])/2
        x = x * (1-s**2) + (self.coord[1] + self.coord[0])/2 * s**2
        y = y * (1-s**2) + (self.coord[3] + self.coord[2])/2 * s**2
        self.coord = [x - xrange * s,
                      x + xrange * s,
                      y - yrange * s,
                      y + yrange * s]      
        
    def animate(self, x, y, file_out, n_frames=100, loop=True):
        """Animated zoom to GIF file
    
        Note that the Mandelbrot object is modified by this function
        
        Args:
            x: float
                real part of point to zoom at
            y: float
                imaginary part of point to zoom at
            file_out: str
                filename to save the GIF output
            n_frames: int
                number of frames in the output file
            loop: boolean
                loop back to original coordinates
        """        
        # Zoom scale: gaussian shape, from 0% (s=1) to 40% (s=0.6)
        # => zoom scale (i.e. speed) is increasing, then decreasing
        def gaussian(n, sig = 1):
            x = np.linspace(-1, 1, n)
            return np.exp(-np.power(x, 2.) / (2 * np.power(sig, 2.)))
        s = 1 - gaussian(n_frames, 1/2)*.4
        
        # Update in case it was not up to date (e.g. parameters changed)
        self.update_set()
        images = [self.set]
        # Making list of images
        for i in range(1, n_frames):
            # Zoom at (x,y)
            self.szoom_at(x,y,s[i])
            # Update the set
            self.update_set()
            images.append(self.set)
            
        # Go backward, one image in two (i.e. 2x speed)
        if(loop):
            images += images[::-2]
        # Make GIF
        imageio.mimsave(file_out, images)   
    
    def explore(self, dpi=72):
        """Run the Mandelbrot explorer: a Matplotlib GUI"""
        # It is important to keep track of the object in a variable, so the
        # slider and button are responsive
        self.explorer = Mandelbrot_explorer(self, dpi)


class Mandelbrot_explorer():
    """A Matplotlib GUI to explore the Mandelbrot set"""
    def __init__(self, mand, dpi=72):
        self.mand = mand
        # Update in case it was not up to date (e.g. parameters changed)
        self.mand.update_set()
        # Plot the set
        self.fig, self.ax = plt.subplots(figsize=(mand.xpixels/dpi,
                                                  mand.ypixels/dpi))
        self.graph = plt.imshow(mand.set,
                                extent=mand.coord, origin='lower')
        plt.subplots_adjust(left=0, right=1, bottom=0, top=1)
        plt.axis('off')
        # Add a slider of number of iterations
        self.ax_sld = plt.axes([0.3, 0.005, 0.4, 0.02])
        self.sld_maxiter = Slider(self.ax_sld, 'Iterations', 0,
                                 max(5000, self.mand.maxiter),
                             valinit=mand.maxiter, valstep=50)
        # Add a button to randomly change the color table
        self.ax_button = plt.axes([0.45, 0.03, 0.1, 0.035])
        self.button = Button(self.ax_button, 'Random colors')
        plt.sca(self.ax)
        
        # Note that it is mandatory to keep track of those objects so they are
        # not deleted by Matplotlib, and callbacks can be used
        # We call the same function for all event: self.onclick
        self.button.on_clicked(self.onclick)
        self.sld_maxiter.on_changed(self.onclick)
        # Responsiveness for any click or scroll
        self.cid1 = self.fig.canvas.mpl_connect('scroll_event', self.onclick)
        self.cid2 = self.fig.canvas.mpl_connect('button_press_event',
                                                self.onclick)
        plt.show()
        
    def onclick(self, event):
        """Event interactivity function"""
        # This function is called by any click/scroll, button click or slider
        # value change
        
        update = False
        
        # If event is an integer: it comes from the Slider
        if(isinstance(event, int)):
            # Slider event: update maxiter
            self.mand.maxiter = event
            update = True
        # Otherwise: check which axe was clicked
        else:    
            if event.inaxes == self.ax:
                # Click or scroll in the main axe: zoom event
                # Default: zoom in
                zoom = 1/2
                if event.button in ('down', 3):
                    # If right click or scroll down: zoom out
                    zoom = 1/zoom
                # Zoom and update figure coordinates
                self.mand.zoom_at(event.xdata, event.ydata, zoom)
                self.graph.set_extent(self.mand.coord)
                update = True
            elif ((event.inaxes == self.ax_button) and
                  (event.name == 'button_press_event')):
                # If the button is pressed: randomly change colortable
                rgb_thetas = np.random.uniform(size=3)
                self.mand.colortable = sin_colortable(rgb_thetas)
                update = True
        if update:
            # Updating the figure
            self.mand.update_set()
            self.graph.set_data(self.mand.set)
            plt.draw()       
            plt.show()


if __name__ == "__main__":
    Mandelbrot().explore()