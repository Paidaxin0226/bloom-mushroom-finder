package com.locwarp.mock;

import android.graphics.Bitmap;
import android.graphics.Color;

import com.google.zxing.BarcodeFormat;
import com.google.zxing.EncodeHintType;
import com.google.zxing.MultiFormatWriter;
import com.google.zxing.common.BitMatrix;

import java.util.EnumMap;
import java.util.Map;

final class QrCodeBitmap {
    private QrCodeBitmap() {
    }

    static Bitmap render(String value, int sizePx) throws Exception {
        Map<EncodeHintType, Object> hints = new EnumMap<>(EncodeHintType.class);
        hints.put(EncodeHintType.MARGIN, 1);
        BitMatrix matrix = new MultiFormatWriter().encode(
            value,
            BarcodeFormat.QR_CODE,
            sizePx,
            sizePx,
            hints
        );
        Bitmap bitmap = Bitmap.createBitmap(sizePx, sizePx, Bitmap.Config.ARGB_8888);
        for (int y = 0; y < sizePx; y++) {
            for (int x = 0; x < sizePx; x++) {
                bitmap.setPixel(x, y, matrix.get(x, y) ? Color.BLACK : Color.WHITE);
            }
        }
        return bitmap;
    }
}
