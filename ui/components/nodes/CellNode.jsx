import React, { memo } from 'react';
import { Handle, Position } from 'reactflow';
import { RadioTower } from 'lucide-react';

const CellNode = ({ data, selected }) => {
  // Eğer çoklu sektör (sectors) verisi gelmişse onu kullan, yoksa eski tekli sistemi kullan (Geriye dönük uyumluluk)
  const sectors = data.sectors && data.sectors.length > 0 
    ? data.sectors 
    : [{ power: data.power, azimuth: data.azimuth }];

  // İZOTROPİK ELEMANDA YÖN YOKTUR. settings.antenna_element 'isotropic' iken
  // dizinin dönüşü tam olarak sadeleşiyor, yani azimut motorda hiçbir şey
  // yapmıyor — o hâlde tuvalde yönlü bir hüzme çizmek, simülasyonun yapmadığı
  // bir şeyin resmini çizmek olur. Bu durumda tek bir eşyönlü halka gösteriyoruz;
  // diğer elemanların (3gpp, cosine, parabolic, circular) gerçek bir deseni var,
  // onlarda koni doğru.
  const omni = (data.antennaElement ?? 'isotropic') === 'isotropic';
  const omniScale = Math.max(
    0.3,
    Math.pow(Math.max(...sectors.map(x => Number(x.power) || 35)) / 30, 1.5),
  );

  return (
    <div className="relative flex items-center justify-center w-10 h-10">

      {omni && (
        <div
          className="absolute rounded-full pointer-events-none z-0"
          title="Isotropic element — no direction. Azimuth has no effect until the antenna model is changed."
          style={{
            width: `${70 * omniScale}px`,
            height: `${70 * omniScale}px`,
            background: 'radial-gradient(circle, rgba(20,184,166,0.22) 0%, rgba(20,184,166,0) 70%)',
            border: '1px dashed rgba(20,184,166,0.35)',
            transition: 'width 0.3s ease, height 0.3s ease',
          }}
        />
      )}

      {/* Her bir sektör (anten) için ayrı bir radar hüzmesi (ışın) çiziyoruz */}
      {!omni && sectors.map((sector, index) => {
        const powerVal = sector.power !== undefined ? Number(sector.power) : 35;
        const scale = Math.max(0.3, Math.pow(powerVal / 30, 1.5)); 
        const azimuth = sector.azimuth !== undefined ? Number(sector.azimuth) : 0;

        return (
          <div 
            key={index}
            className="absolute origin-bottom pointer-events-none z-0"
            style={{
              height: `${70 * scale}px`,
              width: `${50 * scale}px`,
              bottom: '50%',
              background: 'linear-gradient(to top, rgba(20, 184, 166, 0.4), rgba(20, 184, 166, 0))',
              clipPath: 'polygon(50% 100%, 0 0, 100% 0)',
              transform: `rotate(${azimuth}deg)`,
              transformOrigin: 'bottom center',
              transition: 'transform 0.4s cubic-bezier(0.4, 0, 0.2, 1), height 0.3s ease, width 0.3s ease'
            }}
          />
        );
      })}
      
      <div className={`relative z-10 p-2 rounded-full border-2 bg-zinc-950 transition-colors ${selected ? 'border-teal-400 shadow-[0_0_15px_rgba(45,212,191,0.5)]' : 'border-zinc-700'}`}>
        <RadioTower className={`w-5 h-5 ${selected ? 'text-teal-400' : 'text-zinc-400'}`} />
      </div>
      
      <div className="absolute top-full mt-2 px-2 py-0.5 bg-zinc-950 border border-zinc-800 rounded text-[10px] text-zinc-300 font-mono whitespace-nowrap shadow-md z-20">
        {data.label}
      </div>

      <Handle type="source" position={Position.Bottom} className="opacity-0" />
      <Handle type="target" position={Position.Top} className="opacity-0" />
    </div>
  );
};

export default memo(CellNode);
