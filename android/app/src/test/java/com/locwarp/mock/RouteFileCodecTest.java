package com.locwarp.mock;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertTrue;

import org.junit.Test;

import java.util.Arrays;
import java.util.List;

public final class RouteFileCodecTest {
    private final List<LocalSimulationEngine.Point> route = Arrays.asList(
        new LocalSimulationEngine.Point(25.033964d, 121.564468d),
        new LocalSimulationEngine.Point(25.047800d, 121.531900d)
    );

    @Test
    public void gpxRoundTripPreservesPoints() {
        assertRoute(RouteFileCodec.parse(RouteFileCodec.toGpx("test", route)));
    }

    @Test
    public void gpxAcceptsLongitudeBeforeLatitude() {
        List<LocalSimulationEngine.Point> parsed = RouteFileCodec.parse(
            "<gpx><trk><trkseg>"
                + "<trkpt lon=\"121.564468\" lat=\"25.033964\" />"
                + "</trkseg></trk></gpx>"
        );

        assertEquals(1, parsed.size());
        assertEquals(25.033964d, parsed.get(0).lat, 0.0000001d);
        assertEquals(121.564468d, parsed.get(0).lng, 0.0000001d);
    }

    @Test
    public void kmlRoundTripPreservesPoints() {
        assertRoute(RouteFileCodec.parse(RouteFileCodec.toKml("test", route)));
    }

    @Test
    public void geoJsonRoundTripPreservesPoints() {
        String encoded = RouteFileCodec.toGeoJson("test", route);
        assertTrue(encoded.contains("\"LineString\""));
        assertRoute(RouteFileCodec.parse(encoded));
    }

    private void assertRoute(List<LocalSimulationEngine.Point> parsed) {
        assertEquals(2, parsed.size());
        assertEquals(route.get(0).lat, parsed.get(0).lat, 0.0000001d);
        assertEquals(route.get(0).lng, parsed.get(0).lng, 0.0000001d);
        assertEquals(route.get(1).lat, parsed.get(1).lat, 0.0000001d);
        assertEquals(route.get(1).lng, parsed.get(1).lng, 0.0000001d);
    }
}
