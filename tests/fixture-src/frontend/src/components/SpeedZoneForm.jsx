import { useForm } from 'react-hook-form';

export function SpeedZoneForm({ onSave }) {
  const { register, handleSubmit } = useForm();
  return (
    <form id="f" onSubmit={handleSubmit(onSave)}>
      <label>Zone name <input {...register('name', { required: true, maxLength: 80 })} name="name" /></label>
      <label>Speed limit (km/h) <input {...register('speed', { min: 0, max: 130 })} name="speed" type="text" /></label>
      <button data-testid="speed-zone-save">Save</button>
      <p id="msg" role="status" />
    </form>
  );
}
