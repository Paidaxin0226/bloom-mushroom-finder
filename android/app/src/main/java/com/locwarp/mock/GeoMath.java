package com.locwarp.mock;

final class GeoMath {
    private GeoMath() {
    }

    static double[] move(
        double startLat,
        double startLng,
        double bearingDegrees,
        double distanceM
    ) {
        double radius = 6371000d;
        double angular = distanceM / radius;
        double bearing = Math.toRadians(bearingDegrees);
        double lat1 = Math.toRadians(startLat);
        double lng1 = Math.toRadians(startLng);
        double lat2 = Math.asin(
            Math.sin(lat1) * Math.cos(angular)
                + Math.cos(lat1) * Math.sin(angular) * Math.cos(bearing)
        );
        double lng2 = lng1 + Math.atan2(
            Math.sin(bearing) * Math.sin(angular) * Math.cos(lat1),
            Math.cos(angular) - Math.sin(lat1) * Math.sin(lat2)
        );
        double lng = ((Math.toDegrees(lng2) + 540d) % 360d) - 180d;
        return new double[] {Math.toDegrees(lat2), lng};
    }

    static double distanceMeters(double aLat, double aLng, double bLat, double bLng) {
        double radius = 6371000d;
        double lat1 = Math.toRadians(aLat);
        double lat2 = Math.toRadians(bLat);
        double dLat = lat2 - lat1;
        double dLng = Math.toRadians(bLng - aLng);
        double h = Math.sin(dLat / 2d) * Math.sin(dLat / 2d)
            + Math.cos(lat1) * Math.cos(lat2)
            * Math.sin(dLng / 2d) * Math.sin(dLng / 2d);
        return radius * 2d * Math.atan2(Math.sqrt(h), Math.sqrt(1d - h));
    }
}
