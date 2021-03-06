%define majorminor 0.10
%define gstreamer gstreamer

%define gst_minver 0.10.10.1
%define gstpb_minver 0.10.10.1

Summary: GStreamer streaming media framework "ugly" plug-ins
Name: gstreamer-plugins-ugly
#0.10.19 is available but does not build on CentOS 6
Version: 0.10.18
Release: 4%{?dist}
License: LGPL
Group: Applications/Multimedia
URL: http://gstreamer.freedesktop.org/

Source: http://gstreamer.freedesktop.org/src/gst-plugins-ugly/gst-plugins-ugly-%{version}.tar.bz2
BuildRoot: %{_tmppath}/%{name}-%{version}-%{release}-root

BuildRequires: %{gstreamer}-devel >= %{gst_minver}
BuildRequires: %{gstreamer}-plugins-base-devel >= %{gstpb_minver}
Requires: %{gstreamer} >= %{gst_minver}

BuildRequires: gcc-c++
BuildRequires: gettext-devel
BuildRequires: lame-devel >= 3.89
BuildRequires: libmad-devel >= 0.15.0
BuildRequires: liboil-devel

Provides: gstreamer-mad = %{version}-%{release}

%description
GStreamer is a streaming media framework, based on graphs of elements which
operate on media data.

This package contains well-written plug-ins that can't be shipped in
gstreamer-plugins-good because:
- the license is not LGPL
- the license of the library is not LGPL
- there are possible licensing issues with the code.

%prep
%setup -n gst-plugins-ugly-%{version}

%build
%configure \
    --disable-static \
    --disable-amrnb \
    --disable-amrwb \
	--disable-x264 \
    --enable-debug
%{__make} %{?_smp_mflags}

%install
%{__rm} -rf %{buildroot}
%{__make} install DESTDIR="%{buildroot}"

# make output told me to do this
libtool --finish %{buildroot}%{_libdir}/gstreamer-%{majorminor}

%find_lang gst-plugins-ugly-%{majorminor}

# Clean out files that should not be part of the rpm.
%{__rm} -f %{buildroot}%{_libdir}/gstreamer-%{majorminor}/*.la
%{__rm} -f %{buildroot}%{_libdir}/*.la

%clean
%{__rm} -rf %{buildroot}

%files -f gst-plugins-ugly-%{majorminor}.lang
%defattr(-,root,root,-)
%doc AUTHORS COPYING README REQUIREMENTS
%{_libdir}/gstreamer-%{majorminor}/libgstasf.so
%{_libdir}/gstreamer-%{majorminor}/libgstdvdlpcmdec.so
%{_libdir}/gstreamer-%{majorminor}/libgstdvdsub.so
%{_libdir}/gstreamer-%{majorminor}/libgstiec958.so
%{_libdir}/gstreamer-%{majorminor}/libgstlame.so
%{_libdir}/gstreamer-%{majorminor}/libgstmad.so
%{_libdir}/gstreamer-%{majorminor}/libgstmpegaudioparse.so
%{_libdir}/gstreamer-%{majorminor}/libgstmpegstream.so
%{_libdir}/gstreamer-%{majorminor}/libgstrmdemux.so

%changelog
* Fri Oct 17 2014 Antoine Martin <antoine@devloop.org.uk> 0.10.19
- initial xpra package
