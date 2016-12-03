#!/usr/bin/env python3
##############################################################################
# Copyright (c) 2016: Leonardo Cardoso
# https://github.com/LeoFCardoso/pdf2pdfocr
##############################################################################
# OCR a PDF and add a text "layer" in the original file (a so called "pdf sandwich")
# Use only open source tools.
# Unless requested, does not re-encode the images inside an unprotected PDF file.
# Leonardo Cardoso - inspired in ocrmypdf (https://github.com/jbarlow83/OCRmyPDF)
# and this post: https://github.com/jbarlow83/OCRmyPDF/issues/8
#
# pip libraries dependencies: PyPDF2, reportlab
# external tools dependencies: file, poppler, imagemagick, tesseract, ghostscript, pdftk (optional)
###############################################################################
import argparse
import datetime
import errno
import glob
import itertools
import math
import multiprocessing
import os
import random
import shlex
import shutil
import string
import subprocess
import sys
import tempfile
import time

import PyPDF2

####
__author__ = 'Leonardo F. Cardoso'


def eprint(*args, **kwargs):
    print(*args, file=sys.stderr, **kwargs)


def do_pdftoimage(param_path_pdftoppm, param_page_range, param_input_file, param_tmp_dir, param_prefix,
                  param_shell_mode):
    """
    Will be called from multiprocessing, so no global variables are allowed.
    Convert PDF to image file.
    """
    command_line_list = [param_path_pdftoppm]
    first_page = 0
    last_page = 0
    if param_page_range is not None:
        first_page = param_page_range[0]
        last_page = param_page_range[1]
        command_line_list += ['-f', str(first_page), '-l', str(last_page)]
    #
    command_line_list += ['-r', '300', '-jpeg', param_input_file, param_tmp_dir + param_prefix]
    pimage = subprocess.Popen(command_line_list, stdout=subprocess.DEVNULL,
                              stderr=open(
                                  param_tmp_dir + "pdftoppm_err_{0}-{1}-{2}.log".format(param_prefix, first_page,
                                                                                        last_page),
                                  "wb"),
                              shell=param_shell_mode)
    pimage.wait()


def do_deskew(param_image_file, param_threshold, param_shell_mode, param_path_mogrify):
    """
    Will be called from multiprocessing, so no global variables are allowed.
    Do a deskew of image.
    """
    pd = subprocess.Popen([param_path_mogrify, '-deskew', param_threshold, param_image_file], shell=param_shell_mode)
    pd.wait()


def do_ocr(param_image_file, param_tess_lang, param_tess_psm, param_temp_dir, param_shell_mode, param_path_tesseract):
    """
    Will be called from multiprocessing, so no global variables are allowed.
    Do OCR of image.
    """
    # TODO - expert mode - let user pass tesseract custom parameters
    param_image_no_ext = os.path.splitext(os.path.basename(param_image_file))[0]
    pocr = subprocess.Popen([param_path_tesseract, '-l', param_tess_lang,
                             '-c', 'tessedit_create_pdf=1',
                             '-c', 'tessedit_create_txt=1',
                             '-c', 'tessedit_pageseg_mode=' + param_tess_psm,
                             param_image_file,
                             param_temp_dir + param_image_no_ext],
                            stdout=subprocess.DEVNULL,
                            stderr=open(param_temp_dir + "tess_err_{0}.log".format(param_image_no_ext), "wb"),
                            shell=param_shell_mode)
    pocr.wait()
    # New code - uses PDF generated by tesseract with some post processing
    # --------
    pdf_file = param_temp_dir + param_image_no_ext + ".pdf"
    pdf_file_tmp = param_temp_dir + param_image_no_ext + ".tmp"
    os.rename(pdf_file, pdf_file_tmp)
    output_pdf = PyPDF2.PdfFileWriter()
    desc_pdf_file_tmp = open(pdf_file_tmp, 'rb')
    tess_pdf = PyPDF2.PdfFileReader(desc_pdf_file_tmp, strict=False)
    for i in range(tess_pdf.getNumPages()):
        imagepage = tess_pdf.getPage(i)
        output_pdf.addPage(imagepage)
    #
    output_pdf.removeImages(ignoreByteStringObject=False)
    out_page = output_pdf.getPage(0)  # Tesseract PDF is always one page in this software
    # Hack to obtain smaller file (delete the image reference)
    out_page["/Resources"][PyPDF2.generic.createStringObject("/XObject")] = PyPDF2.generic.ArrayObject()
    out_page.compressContentStreams()
    with open(pdf_file, 'wb') as f:
        output_pdf.write(f)
    desc_pdf_file_tmp.close()


def percentual_float(x):
    x = float(x)
    if x <= 0.0 or x > 1.0:
        raise argparse.ArgumentTypeError("%r not in range (0.0, 1.0]" % (x,))
    return x


class Pdf2PdfOcr:

    # External tools command. If you can't edit your path, adjust here to match your system
    cmd_tesseract = "tesseract"
    path_tesseract = ""
    cmd_convert = "convert"
    cmd_magick = "magick"  # used on Windows with ImageMagick 7+ (to avoid conversion path problems)
    path_convert = ""
    cmd_mogrify = "mogrify"
    path_mogrify = ""
    cmd_file = "file"
    path_file = ""
    cmd_pdftk = "pdftk"
    path_pdftk = ""
    cmd_pdftoppm = "pdftoppm"
    path_pdftoppm = ""
    cmd_ps2pdf = "ps2pdf"
    path_ps2pdf = ""
    cmd_pdf2ps = "pdf2ps"
    path_pdf2ps = ""

    extension_images = "jpg"
    """Temp images will use this extension. Using jpg to avoid big temp files in pdf with a lot of pages"""

    output_file = ""
    """The PDF output file"""

    output_file_text = ""
    """The TXT output file"""

    path_this_python = sys.executable
    """Path for python in this system"""

    prefix = ''.join(random.SystemRandom().choice(string.ascii_uppercase + string.digits) for _ in range(5))
    """A random prefix to support multiple execution in parallel"""

    shell_mode = (os.name == 'nt')
    """How to run external process? In Windows use Shell=True
    http://stackoverflow.com/questions/5658622/python-subprocess-popen-environment-path
    "Also, on Windows with shell=False, it pays no attention to PATH at all,
    and will only look in relative to the current working directory."
    """

    tmp_dir = tempfile.gettempdir() + os.path.sep
    """Temp dir"""

    def __init__(self, args):
        super().__init__()
        self.check_external_tools()
        # Handle arguments from command line
        self.safe_mode = args.safe_mode
        self.check_text_mode = args.check_text_mode
        self.check_protection_mode = args.check_protection_mode
        self.force_rebuild_mode = args.force_rebuild_mode
        self.user_convert_params = args.convert_params
        if self.user_convert_params is None:
            self.user_convert_params = ""  # Default
        self.deskew_threshold = args.deskew_percent
        self.use_deskew_mode = args.deskew_percent is not None
        self.parallel_threshold = args.parallel_percent
        if self.parallel_threshold is None:
            self.parallel_threshold = 1  # Default
        self.create_text_mode = args.create_text_mode
        self.force_out_mode = args.output_file is not None
        if self.force_out_mode:
            self.force_out_file = args.output_file
        else:
            self.force_out_file = ""
        self.tess_langs = args.tess_langs
        if self.tess_langs is None:
            self.tess_langs = "por+eng"  # Default
        self.tess_psm = args.tess_psm
        if self.tess_psm is None:
            self.tess_psm = "1"  # Default
        self.delete_temps = not args.keep_temps
        self.verbose_mode = args.verbose_mode
        self.input_file = args.input_file
        if not os.path.isfile(self.input_file):
            eprint("{0} not found. Exiting.".format(self.input_file))
            exit(1)
        self.input_file = os.path.abspath(self.input_file)
        self.input_file_type = ""
        #
        self.use_pdftk = args.use_pdftk
        if self.use_pdftk:
            self.path_pdftk = shutil.which(self.cmd_pdftk)
            if self.path_pdftk is None:
                eprint("pdftk not found. Aborting...")
                exit(1)
        #
        self.input_file_has_text = False
        self.input_file_is_encrypted = False
        self.input_file_metadata = dict()
        self.input_file_number_of_pages = None
        #
        self.debug("Temp dir is {0}".format(self.tmp_dir))
        self.debug("Prefix is {0}".format(self.prefix))
        # Where am I?
        self.script_dir = os.path.dirname(os.path.abspath(__file__)) + os.path.sep
        self.debug("Script dir is {0}".format(self.script_dir))
        #
        self.cpu_to_use = int(multiprocessing.cpu_count() * self.parallel_threshold)
        if self.cpu_to_use == 0:
            self.cpu_to_use = 1
        self.debug("Parallel operations will use {0} CPUs".format(self.cpu_to_use))
        #

    def check_external_tools(self):
        """Check if external tools are available, aborting in case of any error."""
        self.path_tesseract = shutil.which(self.cmd_tesseract)
        if self.path_tesseract is None:
            eprint("tesseract not found. Aborting...")
            exit(1)
        # Try to avoid errors on Windows with native OS "convert" command
        # http://savage.net.au/ImageMagick/html/install-convert.html
        # https://www.imagemagick.org/script/magick.php
        self.path_convert = shutil.which(self.cmd_convert)
        if not self.test_convert():
            self.path_convert = shutil.which(self.cmd_magick)
        if self.path_convert is None:
            eprint("convert/magick from ImageMagick not found. Aborting...")
            exit(1)
        #
        self.path_mogrify = shutil.which(self.cmd_mogrify)
        if self.path_mogrify is None:
            eprint("mogrify from ImageMagick not found. Aborting...")
            exit(1)
        #
        self.path_file = shutil.which(self.cmd_file)
        if self.path_file is None:
            eprint("file not found. Aborting...")
            exit(1)
        #
        self.path_pdftoppm = shutil.which(self.cmd_pdftoppm)
        if self.path_pdftoppm is None:
            eprint("pdftoppm (poppler) not found. Aborting...")
            exit(1)
        #
        self.path_ps2pdf = shutil.which(self.cmd_ps2pdf)
        self.path_pdf2ps = shutil.which(self.cmd_pdf2ps)
        if self.path_ps2pdf is None or self.path_pdf2ps is None:
            eprint("ps2pdf or pdf2ps (ghostscript) not found. File repair will not work...")

    def debug(self, param):
        try:
            if self.verbose_mode:
                tstamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')
                print("[{0}] [DEBUG]\t{1}".format(tstamp, param))
        except:
            pass

    def log(self, param):
        try:
            tstamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')
            print("[{0}] [LOG]\t{1}".format(tstamp, param))
        except:
            pass

    def cleanup(self):
        if self.delete_temps:
            # All with PREFIX on temp files
            for f in glob.glob(self.tmp_dir + "*" + self.prefix + "*.*"):
                os.remove(f)
        else:
            eprint("Temporary files kept in {0}".format(self.tmp_dir))

    def ocr(self):
        self.log("Welcome to pdf2pdfocr version {0}".format(version))
        self.detect_file_type()
        if self.input_file_type == "application/pdf":
            self.validate_pdf_input_file()
        self.define_output_files()
        self.initial_cleanup()
        self.convert_input_to_images()
        image_file_list = sorted(glob.glob(self.tmp_dir+"{0}*.{1}".format(self.prefix, self.extension_images)))
        self.deskew(image_file_list)
        self.external_ocr(image_file_list)
        self.join_ocred_pdf()
        self.create_text_output()
        self.build_final_output()
        # TODO - create option for PDF/A files
        # gs -dPDFA=3 -dBATCH -dNOPAUSE -sProcessColorModel=DeviceCMYK -sDEVICE=pdfwrite
        # -sPDFACompatibilityPolicy=2 -sOutputFile=output_filename.pdf ./Test.pdf
        # As in
        # http://git.ghostscript.com/?p=ghostpdl.git;a=blob_plain;f=doc/VectorDevices.htm;hb=HEAD#PDFA
        #
        # Edit producer and build final PDF
        # Without edit producer is easy as "shutil.copyfile(tmp_dir + prefix + "-OUTPUT.pdf", output_file)"
        self.edit_producer()
        #
        self.debug("Output file created")
        #
        # Adjust the new file timestamp
        # TODO touch -r "$INPUT_FILE" "$OUTPUT_FILE"
        #
        self.cleanup()
        #
        paypal_donate_link = "https://www.paypal.com/cgi-bin/webscr?cmd=_donations&business=leonardo%2ef%2ecardoso%40gmail%2ecom&lc=US&item_name=pdf2pdfocr%20development&currency_code=USD&bn=PP%2dDonationsBF%3abtn_donateCC_LG%2egif%3aNonHosted"
        flattr_donate_link = "https://flattr.com/profile/pdf2pdfocr.devel"
        success_message = """Success!
This software is free, but if you like it, please donate to support new features.
---> Paypal
{0}
---> Flattr
{1}""".format(paypal_donate_link, flattr_donate_link)
        self.log(success_message)

    def build_final_output(self):
        # Start building final PDF.
        # First, should we rebuild source file?
        rebuild_pdf_from_images = False
        if self.input_file_is_encrypted or self.input_file_type != "application/pdf" or self.use_deskew_mode:
            rebuild_pdf_from_images = True
        #
        if (not rebuild_pdf_from_images) and (not self.force_rebuild_mode):
            # Merge OCR background PDF into the main PDF document making a PDF sandwich
            if self.use_pdftk:
                self.debug("Merging with OCR with pdftk")
                ppdftk = subprocess.Popen(
                    [self.path_pdftk, self.input_file, 'multibackground', self.tmp_dir + self.prefix + "-ocr.pdf",
                     'output', self.tmp_dir + self.prefix + "-OUTPUT.pdf"], stdout=subprocess.DEVNULL,
                    stderr=open(self.tmp_dir + "err_multiback-{0}-merge-pdftk.log".format(self.prefix),
                                "wb"), shell=self.shell_mode)
                ppdftk.wait()
            else:
                self.debug("Merging with OCR")
                pmulti = subprocess.Popen(
                    [self.path_this_python, self.script_dir + 'pdf2pdfocr_multibackground.py', self.input_file,
                     self.tmp_dir + self.prefix + "-ocr.pdf", self.tmp_dir + self.prefix + "-OUTPUT.pdf"],
                    stdout=subprocess.DEVNULL,
                    stderr=open(self.tmp_dir + "err_multiback-{0}-merge.log".format(self.prefix), "wb"),
                    shell=self.shell_mode)
                pmulti.wait()
                # Sometimes, the above script fail with some malformed input PDF files.
                # The code below try to rewrite source PDF and run it again.
                if not os.path.isfile(self.tmp_dir + self.prefix + "-OUTPUT.pdf"):
                    self.try_repair_input_and_merge()
        else:
            self.rebuild_and_merge()
        #
        if not os.path.isfile(self.tmp_dir + self.prefix + "-OUTPUT.pdf"):
            eprint("Output file could not be created :( Exiting with error code.")
            self.cleanup()
            exit(1)

    def rebuild_and_merge(self):
        eprint("Warning: metadata wiped from final PDF file (original file is not an unprotected PDF / "
               "forcing rebuild from extracted images / using deskew)")
        # Convert presets
        # Please read http://www.imagemagick.org/Usage/quantize/#colors_two
        preset_fast = "-threshold 60% -compress Group4"
        preset_best = "-colors 2 -colorspace gray -normalize -threshold 60% -compress Group4"
        preset_grayscale = "-threshold 85% -morphology Dilate Diamond -compress Group4"
        preset_jpeg = "-strip -interlace Plane -gaussian-blur 0.05 -quality 50% -compress JPEG"
        preset_jpeg2000 = "-quality 32% -compress JPEG2000"
        #
        convert_params = ""
        if self.user_convert_params == "fast":
            convert_params = preset_fast
        elif self.user_convert_params == "best":
            convert_params = preset_best
        elif self.user_convert_params == "grasyscale":
            convert_params = preset_grayscale
        elif self.user_convert_params == "jpeg":
            convert_params = preset_jpeg
        elif self.user_convert_params == "jpeg2000":
            convert_params = preset_jpeg2000
        else:
            convert_params = self.user_convert_params
        # Handle default case
        if convert_params == "":
            convert_params = preset_best
        #
        # http://stackoverflow.com/questions/79968/split-a-string-by-spaces-preserving-quoted-substrings-in-python
        self.log("Rebuilding PDF from images")
        convert_params_list = shlex.split(convert_params)
        prebuild = subprocess.Popen(
            [self.path_convert] + sorted(
                glob.glob(self.tmp_dir + self.prefix + "*." + self.extension_images)) + convert_params_list + [
                self.tmp_dir + self.prefix + "-input_unprotected.pdf"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=self.shell_mode)
        prebuild.wait()
        #
        self.debug("Merging with OCR")
        pmulti = subprocess.Popen([self.path_this_python, self.script_dir + 'pdf2pdfocr_multibackground.py',
                                   self.tmp_dir + self.prefix + "-input_unprotected.pdf",
                                   self.tmp_dir + self.prefix + "-ocr.pdf",
                                   self.tmp_dir + self.prefix + "-OUTPUT.pdf"],
                                  stdout=subprocess.DEVNULL,
                                  stderr=open(self.tmp_dir + "err_multiback-{0}-rebuild.log".format(self.prefix),
                                              "wb"),
                                  shell=self.shell_mode)
        pmulti.wait()

    def try_repair_input_and_merge(self):
        self.debug(
            "Fail to merge source PDF with extracted OCR text. Trying to fix source PDF to build final file...")
        prepair1 = subprocess.Popen(
            [self.path_pdf2ps, self.input_file, self.tmp_dir + self.prefix + "-fixPDF.ps"],
            stdout=subprocess.DEVNULL,
            stderr=open(self.tmp_dir + "err_pdf2ps-{0}.log".format(self.prefix), "wb"),
            shell=self.shell_mode)
        prepair1.wait()
        prepair2 = subprocess.Popen([self.path_ps2pdf, self.tmp_dir + self.prefix + "-fixPDF.ps",
                                     self.tmp_dir + self.prefix + "-fixPDF.pdf"],
                                    stdout=subprocess.DEVNULL,
                                    stderr=open(self.tmp_dir + "err_ps2pdf-{0}.log".format(self.prefix),
                                                "wb"), shell=self.shell_mode)
        prepair2.wait()
        pmulti2 = subprocess.Popen(
            [self.path_this_python, self.script_dir + 'pdf2pdfocr_multibackground.py',
             self.tmp_dir + self.prefix + "-fixPDF.pdf",
             self.tmp_dir + self.prefix + "-ocr.pdf", self.tmp_dir + self.prefix + "-OUTPUT.pdf"],
            stdout=subprocess.DEVNULL,
            stderr=open(self.tmp_dir + "err_multiback-{0}-merge-fixed.log".format(self.prefix),
                        "wb"),
            shell=self.shell_mode)
        pmulti2.wait()
        #

    def create_text_output(self):
        # Create final text output
        if self.create_text_mode:
            text_files = sorted(glob.glob(self.tmp_dir + self.prefix + "*.txt"))
            text_io_wrapper = open(self.output_file_text, 'wb')
            with text_io_wrapper as outfile:
                for fname in text_files:
                    with open(fname, 'rb') as infile:
                        outfile.write(infile.read())
            #
            text_io_wrapper.close()
            #
            self.log("Created final text file")

    def join_ocred_pdf(self):
        # Join PDF files into one file that contains all OCR "backgrounds"
        # Workaround for bug 72720 in older poppler releases
        # https://bugs.freedesktop.org/show_bug.cgi?id=72720
        text_pdf_file_list = sorted(glob.glob(self.tmp_dir + "{0}*.{1}".format(self.prefix, "pdf")))
        self.debug("We have {0} ocr'ed files".format(len(text_pdf_file_list)))
        if len(text_pdf_file_list) > 1:
            pdf_merger = PyPDF2.PdfFileMerger()
            for text_pdf_file in text_pdf_file_list:
                pdf_merger.append(PyPDF2.PdfFileReader(text_pdf_file, strict=False))
            pdf_merger.write(self.tmp_dir + self.prefix + "-ocr.pdf")
            pdf_merger.close()
        else:
            if len(text_pdf_file_list) == 1:
                shutil.copyfile(text_pdf_file_list[0], self.tmp_dir + self.prefix + "-ocr.pdf")
            else:
                eprint("No PDF files generated after OCR. This is not expected. Aborting.")
                self.cleanup()
                exit(1)
        #
        self.debug("Joined ocr'ed PDF files")

    def external_ocr(self, image_file_list):
        self.log("Starting OCR...")
        ocr_pool = multiprocessing.Pool(self.cpu_to_use)
        ocr_pool_map = ocr_pool.starmap_async(do_ocr,
                                              zip(image_file_list, itertools.repeat(self.tess_langs),
                                                  itertools.repeat(self.tess_psm), itertools.repeat(self.tmp_dir),
                                                  itertools.repeat(self.shell_mode),
                                                  itertools.repeat(self.path_tesseract)))
        while not ocr_pool_map.ready():
            pages_processed = len(glob.glob(self.tmp_dir + self.prefix + "*.tmp"))
            self.log("Waiting for OCR to complete. {0} pages completed...".format(pages_processed))
            time.sleep(5)
        #
        self.log("OCR completed")

    def deskew(self, image_file_list):
        if self.use_deskew_mode:
            self.debug("Applying deskew (will rebuild final PDF file)")
            deskew_pool = multiprocessing.Pool(self.cpu_to_use)
            deskew_pool.starmap(do_deskew, zip(image_file_list, itertools.repeat(self.deskew_threshold),
                                               itertools.repeat(self.shell_mode), itertools.repeat(self.path_mogrify)))
            # Sequential code below
            # for image_file in deskew_file_list:
            #     do_deskew(...)
        #

    def convert_input_to_images(self):
        self.log("Converting input file to images...")
        if self.input_file_type == "application/pdf":
            parallel_page_ranges = self.calculate_ranges()
            if parallel_page_ranges is not None:
                pdfimage_pool = multiprocessing.Pool(self.cpu_to_use)
                # TODO - try to use function inside this class
                pdfimage_pool.starmap(do_pdftoimage, zip(itertools.repeat(self.path_pdftoppm),
                                                         parallel_page_ranges,
                                                         itertools.repeat(self.input_file),
                                                         itertools.repeat(self.tmp_dir),
                                                         itertools.repeat(self.prefix),
                                                         itertools.repeat(self.shell_mode)))
            else:
                # Without page info, only alternative is going sequentialy (without range)
                do_pdftoimage(self.path_pdftoppm, None, self.input_file, self.tmp_dir, self.prefix, self.shell_mode)
        else:
            if self.input_file_type in ["image/tiff", "image/jpeg", "image/png"]:
                # %09d to format files for correct sort
                p = subprocess.Popen([self.path_convert, self.input_file, '-quality', '100', '-scene', '1',
                                      self.tmp_dir + self.prefix + '-%09d.' + self.extension_images],
                                     shell=self.shell_mode)
                p.wait()
            else:
                eprint("{0} is not supported in this script. Exiting.".format(self.input_file_type))
                self.cleanup()
                exit(1)

    def initial_cleanup(self):
        Pdf2PdfOcr.best_effort_remove(self.output_file)
        if self.create_text_mode:
            Pdf2PdfOcr.best_effort_remove(self.output_file_text)

    def define_output_files(self):
        if self.force_out_mode:
            self.output_file = self.force_out_file
        else:
            output_name_no_ext = os.path.splitext(os.path.basename(self.input_file))[0]
            output_dir = os.path.dirname(self.input_file)
            self.output_file = output_dir + os.path.sep + output_name_no_ext + "-OCR.pdf"
        #
        self.output_file_text = self.output_file + ".txt"
        self.debug("Output file: {0} for PDF and {1} for TXT".format(self.output_file, self.output_file_text))
        if (self.safe_mode and os.path.isfile(self.output_file)) or \
                (self.safe_mode and self.create_text_mode and os.path.isfile(self.output_file_text)):
            if os.path.isfile(self.output_file):
                eprint("{0} already exists and safe mode is enabled. Exiting.".format(self.output_file))
            if self.create_text_mode and os.path.isfile(self.output_file_text):
                eprint("{0} already exists and safe mode is enabled. Exiting.".format(self.output_file_text))
            self.cleanup()
            exit(1)

    def validate_pdf_input_file(self):
        try:
            pdfFileObj = open(self.input_file, 'rb')
            pdfReader = PyPDF2.PdfFileReader(pdfFileObj, strict=False)
        except PyPDF2.utils.PdfReadError:
            eprint("Corrupted PDF file detected. Aborting...")
            self.cleanup()
            exit(1)
        #
        try:
            self.input_file_number_of_pages = pdfReader.getNumPages()
        except Exception:
            eprint("Warning: could not read input file number of pages.")
            self.input_file_number_of_pages = None
        #
        self.input_file_is_encrypted = pdfReader.isEncrypted
        if not self.input_file_is_encrypted:
            self.input_file_metadata = pdfReader.documentInfo
        text_check_failed = False
        try:
            fonts = set()
            embedded = set()
            for pageObj in pdfReader.pages:
                try:
                    # Test fonts for page
                    f, e = Pdf2PdfOcr.walk(pageObj['/Resources'], fonts, embedded)
                    fonts = fonts.union(f)
                    embedded = embedded.union(e)
                    if len(fonts.union(embedded)) != 0:
                        self.input_file_has_text = True
                        break
                except TypeError:
                    text_check_failed = True
        except PyPDF2.utils.PdfReadError:
            text_check_failed = True
        #
        if self.check_text_mode and text_check_failed and not self.input_file_has_text:
            eprint("Warning: fail to check for text in input file. Assuming no text, but this can be wrong")
        #
        if self.input_file_type == "application/pdf" and self.check_text_mode and self.input_file_has_text:
            eprint("{0} already has text and check text mode is enabled. Exiting.".format(self.input_file))
            self.cleanup()
            exit(1)
        #
        if self.input_file_type == "application/pdf" and self.check_protection_mode and self.input_file_is_encrypted:
            eprint("{0} is encrypted PDF and check encryption mode is enabled. Exiting.".format(self.input_file))
            self.cleanup()
            exit(1)

    def detect_file_type(self):
        """Detect mime type of input file"""
        pfile = subprocess.Popen([self.path_file, '-b', '--mime-type', self.input_file], stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, shell=self.shell_mode)
        pfile_output, pfile_errors = pfile.communicate()
        pfile.wait()
        self.input_file_type = pfile_output.decode("utf-8").strip()
        self.log("Input file {0}: type is {1}".format(self.input_file, self.input_file_type))

    def test_convert(self):
        """
        test convert command to check if it's ImageMagick
        :return: True if it's ImageMagicks convert, false with any other case
        """
        result = False
        test_image = self.tmp_dir + "converttest-" + self.prefix + ".jpg"
        ptest = subprocess.Popen([self.path_convert, 'rose:', test_image], stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, shell=self.shell_mode)
        streamdata = ptest.communicate()[0]
        ptest.wait()
        return_code = ptest.returncode
        if return_code == 0:
            Pdf2PdfOcr.best_effort_remove(test_image)
            result = True
        return result

    def calculate_ranges(self):
        """
        calculate ranges to run pdftoppm in parallel. Each CPU available will run well defined page range
        :return:
        """
        if self.input_file_number_of_pages is None:
            return None
        #
        range_size = math.ceil(self.input_file_number_of_pages / self.cpu_to_use)
        number_of_ranges = math.ceil(self.input_file_number_of_pages / range_size)
        result = []
        for i in range(0, number_of_ranges):
            range_start = (range_size * i) + 1
            range_end = (range_size * i) + range_size
            # Handle last range
            if range_end > self.input_file_number_of_pages:
                range_end = self.input_file_number_of_pages
            result.append((range_start, range_end))
        # Check result
        check_pages = 0
        for created_range in result:
            check_pages += (created_range[1] - created_range[0]) + 1
        if check_pages != self.input_file_number_of_pages:
            raise ArithmeticError("Please check 'calculate_ranges' function, something is wrong...")
        #
        return result

    def edit_producer(self):
        self.debug("Editing producer")
        param_source_file = self.tmp_dir + self.prefix + "-OUTPUT.pdf"
        file_source = open(param_source_file, 'rb')
        pre_output_pdf = PyPDF2.PdfFileReader(file_source, strict=False)
        final_output_pdf = PyPDF2.PdfFileWriter()
        for i in range(pre_output_pdf.getNumPages()):
            page = pre_output_pdf.getPage(i)
            final_output_pdf.addPage(page)
        info_dict_output = dict()
        # Our signature as a producer
        our_name = "PDF2PDFOCR(github.com/LeoFCardoso/pdf2pdfocr)"
        read_producer = False
        producer_key = "/Producer"
        if self.input_file_metadata is not None:
            for key in self.input_file_metadata:
                value = self.input_file_metadata[key]
                if key == producer_key:
                    value = value + "; " + our_name
                    read_producer = True
                #
                try:
                    # Check if value can be accepted by pypdf API
                    test_conversion = PyPDF2.generic.createStringObject(value)
                    info_dict_output[key] = value
                except TypeError:
                    # This can happen with some array properties.
                    eprint("Warning: property " + key + " not copied to final PDF")
        #
        if not read_producer:
            info_dict_output[producer_key] = our_name
        #
        final_output_pdf.addMetadata(info_dict_output)
        #
        with open(self.output_file, 'wb') as f:
            final_output_pdf.write(f)
            f.close()
        #
        file_source.close()

    # Based on https://gist.github.com/tiarno/8a2995e70cee42f01e79
    # -> find PDF font info with PyPDF2, example code
    @staticmethod
    def walk(obj, fnt, emb):
        """
        If there is a key called 'BaseFont', that is a font that is used in the document.
        If there is a key called 'FontName' and another key in the same dictionary object
        that is called 'FontFilex' (where x is null, 2, or 3), then that fontname is
        embedded.
        We create and add to two sets, fnt = fonts used and emb = fonts embedded.
        """
        if not hasattr(obj, 'keys'):
            return None, None
        fontkeys = {'/FontFile', '/FontFile2', '/FontFile3'}
        if '/BaseFont' in obj:
            fnt.add(obj['/BaseFont'])
        if '/FontName' in obj:
            if [x for x in fontkeys if x in obj]:  # test to see if there is FontFile
                emb.add(obj['/FontName'])
        for k in obj.keys():
            Pdf2PdfOcr.walk(obj[k], fnt, emb)
        return fnt, emb  # return the sets for each page

    @staticmethod
    def best_effort_remove(filename):
        try:
            os.remove(filename)
        except OSError as e:
            if e.errno != errno.ENOENT:  # errno.ENOENT = no such file or directory
                raise  # re-raise exception if a different error occured


# -------------
# MAIN
# -------------
if __name__ == '__main__':
    # https://docs.python.org/3/library/multiprocessing.html#multiprocessing-programming
    # See "Safe importing of main module"
    multiprocessing.freeze_support()  # Should make effect only on non-fork systems (Windows)
    version = '1.0.5'
    # Arguments
    parser = argparse.ArgumentParser(description=('pdf2pdfocr.py version %s (http://semver.org/lang/pt-BR/)' % version),
                                     formatter_class=argparse.RawTextHelpFormatter)
    requiredNamed = parser.add_argument_group('required arguments')
    requiredNamed.add_argument("-i", dest="input_file", action="store", required=True,
                               help="path for input file")
    #
    parser.add_argument("-s", dest="safe_mode", action="store_true", default=False,
                        help="safe mode. Does not overwrite output [PDF | TXT] OCR file")
    parser.add_argument("-t", dest="check_text_mode", action="store_true", default=False,
                        help="check text mode. Does not process if source PDF already has text")
    parser.add_argument("-a", dest="check_protection_mode", action="store_true", default=False,
                        help="check encryption mode. Does not process if source PDF is protected")
    parser.add_argument("-f", dest="force_rebuild_mode", action="store_true", default=False,
                        help="force PDF rebuild from extracted images")
    # Escape % wiht %%
    option_g_help = """with images or '-f', use presets or force parameters when calling 'convert' to build the final PDF file
Examples:
    -g fast -> a fast bitonal file ("-threshold 60%% -compress Group4")
    -g best -> best quality, but bigger bitonal file ("-colors 2 -colorspace gray -normalize -threshold 60%% -compress Group4")
    -g grayscale -> good bitonal file from grayscale documents ("-threshold 85%% -morphology Dilate Diamond -compress Group4")
    -g jpeg -> keep original color image as JPEG ("-strip -interlace Plane -gaussian-blur 0.05 -quality 50%% -compress JPEG")
    -g jpeg2000 -> keep original color image as JPEG2000 ("-quality 32%% -compress JPEG2000")
    -g "-threshold 60%% -compress Group4" -> direct apply these parameters (DON'T FORGET TO USE QUOTATION MARKS)
    Note, without -g, preset 'best' is used"""
    parser.add_argument("-g", dest="convert_params", action="store", default="",
                        help=option_g_help)
    parser.add_argument("-d", dest="deskew_percent", action="store",
                        help="use imagemagick deskew *before* OCR. <DESKEW_PERCENT> should be a percent, e.g. '40%%'")
    parser.add_argument("-j", dest="parallel_percent", action="store", type=percentual_float,
                        help="run this percentual jobs in parallel (0 - 1.0] - multiply with the number of CPU cores"
                             " (default = 1 [all cores])")
    parser.add_argument("-w", dest="create_text_mode", action="store_true", default=False,
                        help="also create a text file at same location of PDF OCR file")
    parser.add_argument("-o", dest="output_file", action="store", required=False,
                        help="force output file to the specified location")
    parser.add_argument("-p", dest="use_pdftk", action="store_true", default=False,
                        help="force the use of pdftk tool to do the final overlay of files "
                             "(if not rebuild from images)")
    parser.add_argument("-l", dest="tess_langs", action="store", required=False,
                        help="force tesseract to use specific languages (default: por+eng)")
    parser.add_argument("-m", dest="tess_psm", action="store", required=False,
                        help="force tesseract to use HOCR with specific \"pagesegmode\" (default: tesseract "
                             "HOCR default = 1). Use with caution")
    parser.add_argument("-k", dest="keep_temps", action="store_true", default=False,
                        help="keep temporary files for debug")
    parser.add_argument("-v", dest="verbose_mode", action="store_true", default=False,
                        help="enable verbose mode")
    #
    args = parser.parse_args()
    #
    pdf2ocr = Pdf2PdfOcr(args)
    pdf2ocr.ocr()
    #
    exit(0)
    #
# This is the end
